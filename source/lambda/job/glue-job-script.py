import datetime
import functools
import itertools
import json
import logging
import os
import sys
import time
import traceback
from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple

import boto3
import chardet
import nltk
from boto3.dynamodb.conditions import Attr, Key
from langchain.docstore.document import Document
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import OpenSearchVectorSearch
from opensearchpy import RequestsHttpConnection
from requests_aws4auth import AWS4Auth
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger()
logger.setLevel(logging.INFO)

try:
    from awsglue.utils import getResolvedOptions

    args = getResolvedOptions(
        sys.argv,
        [
            "AOS_ENDPOINT",
            "BATCH_FILE_NUMBER",
            "BATCH_INDICE",
            "EMBEDDING_MODEL_ENDPOINT",
            "ETL_MODEL_ENDPOINT",
            "JOB_NAME",
            "OFFLINE",
            "ProcessedObjectsTable",
            "QA_ENHANCEMENT",
            "REGION",
            "RES_BUCKET",
            "S3_BUCKET",
            "S3_PREFIX",
            "WORKSPACE_ID",
            "WORKSPACE_TABLE",
            "INDEX_TYPE",
            "OPERATION_TYPE",
        ],
    )
except Exception as e:
    logger.warning("Running locally")
    sys.path.append("dep")
    args = json.load(open(sys.argv[1]))
    args["BATCH_INDICE"] = sys.argv[2]

from llm_bot_dep import sm_utils
from llm_bot_dep.constant import SplittingType
from llm_bot_dep.ddb_utils import WorkspaceManager
from llm_bot_dep.embeddings import get_embedding_info
from llm_bot_dep.enhance_utils import EnhanceWithBedrock
from llm_bot_dep.loaders.auto import cb_process_object
from llm_bot_dep.storage_utils import save_content_to_s3

# Adaption to allow nougat to run in AWS Glue with writable /tmp
os.environ["TRANSFORMERS_CACHE"] = "/tmp/transformers_cache"
os.environ["NOUGAT_CHECKPOINT"] = "/tmp/nougat_checkpoint"
os.environ["NLTK_DATA"] = "/tmp/nltk_data"

# Parse arguments
if "BATCH_INDICE" not in args:
    args["BATCH_INDICE"] = "0"

aosEndpoint = args["AOS_ENDPOINT"]
batchFileNumber = args["BATCH_FILE_NUMBER"]
batchIndice = args["BATCH_INDICE"]
embeddingModelEndpoint = args["EMBEDDING_MODEL_ENDPOINT"]
etlModelEndpoint = args["ETL_MODEL_ENDPOINT"]
offline = args["OFFLINE"]
processedObjectsTable = args["ProcessedObjectsTable"]
qa_enhancement = args["QA_ENHANCEMENT"]
region = args["REGION"]
res_bucket = args["RES_BUCKET"]
s3_bucket = args["S3_BUCKET"]
s3_prefix = args["S3_PREFIX"]
workspace_id = args["WORKSPACE_ID"]
workspace_table = args["WORKSPACE_TABLE"]
index_type = args["INDEX_TYPE"]
# Valid Opeartion types: "create", "delete", "update"
operation_type = args["OPERATION_TYPE"]


s3_client = boto3.client("s3")
smr_client = boto3.client("sagemaker-runtime")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(processedObjectsTable)
workspace_table = dynamodb.Table(workspace_table)
workspace_manager = WorkspaceManager(workspace_table)

ENHANCE_CHUNK_SIZE = 25000
OBJECT_EXPIRY_TIME = 3600

credentials = boto3.Session().get_credentials()
awsauth = AWS4Auth(
    credentials.access_key,
    credentials.secret_key,
    region,
    "es",
    session_token=credentials.token,
)
MAX_OS_DOCS_PER_PUT = 8

nltk.data.path.append("/tmp/nltk_data")


class S3FileProcessor:
    def __init__(self, bucket: str, prefix: str, supported_file_types: List[str] = []):
        self.bucket = bucket
        self.prefix = prefix
        self.supported_file_types = supported_file_types
        self.paginator = s3_client.get_paginator("list_objects_v2")

    def get_file_content(self, key: str):
        response = s3_client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()

    def process_file(self, key: str, file_type: str, file_content: str):
        kwargs = {
            "bucket": self.bucket,
            "key": key,
            "etl_model_endpoint": etlModelEndpoint,
            "smr_client": smr_client,
            "res_bucket": res_bucket,
        }

        if file_type == "txt":
            return "txt", self.decode_file_content(file_content), kwargs
        elif file_type == "csv":
            kwargs["csv_row_count"] = 1
            return "csv", self.decode_file_content(file_content), kwargs
        elif file_type == "html":
            return "html", self.decode_file_content(file_content), kwargs
        elif file_type in ["pdf"]:
            return "pdf", file_content, kwargs
        elif file_type in ["jpg", "png"]:
            return "image", file_content, kwargs
        elif file_type in ["docx", "doc"]:
            return "doc", file_content, kwargs
        elif file_type == "md":
            return "md", self.decode_file_content(file_content), kwargs
        elif file_type == "json":
            return "json", self.decode_file_content(file_content), kwargs
        elif file_type == "jsonl":
            return "jsonl", file_content, kwargs
        else:
            logger.info(f"Unknown file type: {file_type}")

    def decode_file_content(self, file_content: str, default_encoding: str = "utf-8"):
        """Decode the file content and auto detect the content encoding.

        Args:
            content: The content to detect the encoding.
            default_encoding: The default encoding to try to decode the content.
            timeout: The timeout in seconds for the encoding detection.
        """
        try:
            decoded_content = file_content.decode(default_encoding)
        except UnicodeDecodeError:
            # Try to detect encoding
            encoding = chardet.detect(file_content)["encoding"]
            decoded_content = file_content.decode(encoding)

        return decoded_content

    def iterate_s3_files(self, extract_content=True) -> Generator:
        currentIndice = 0
        for page in self.paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                file_type = key.split(".")[-1].lower()  # Extract file extension

                if key.endswith("/") or file_type not in self.supported_file_types:
                    continue

                if currentIndice < int(batchIndice) * int(batchFileNumber):
                    continue
                elif currentIndice >= (int(batchIndice) + 1) * int(batchFileNumber):
                    # Exit this nested loop
                    break
                else:
                    logger.info("Processing object: %s", key)

                    if extract_content:
                        file_content = self.get_file_content(key)
                        yield self.process_file(key, file_type, file_content)
                    else:
                        yield file_type, "", {"bucket": self.bucket, "key": key}

                currentIndice += 1
            if currentIndice >= (int(batchIndice) + 1) * int(batchFileNumber):
                # Exit the outer loop
                break


class BatchChunkDocumentProcessor:
    def __init__(
        self,
        chunk_size: int,
        chunk_overlap: int,
        batch_size: int,
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.batch_size = batch_size

    def chunk_generator(
        self, content: List[Document]
    ) -> Generator[Document, None, None]:
        temp_text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap
        )
        temp_content = content
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap
        )
        updated_heading_hierarchy = {}
        for temp_document in temp_content:
            temp_chunk_id = temp_document.metadata["chunk_id"]
            temp_split_size = len(temp_text_splitter.split_documents([temp_document]))
            # Add size in heading_hierarchy
            if "heading_hierarchy" in temp_document.metadata:
                temp_hierarchy = temp_document.metadata["heading_hierarchy"]
                temp_hierarchy["size"] = temp_split_size
                updated_heading_hierarchy[temp_chunk_id] = temp_hierarchy

        for document in content:
            splits = text_splitter.split_documents([document])
            # list of Document objects
            index = 1
            for split in splits:
                chunk_id = split.metadata["chunk_id"]
                logger.info(chunk_id)
                split.metadata["chunk_id"] = f"{chunk_id}-{index}"
                if chunk_id in updated_heading_hierarchy:
                    split.metadata["heading_hierarchy"] = updated_heading_hierarchy[
                        chunk_id
                    ]
                    logger.info(split.metadata["heading_hierarchy"])
                index += 1
                yield split

    def batch_generator(self, content: List[Document], gen_chunk_flag: bool = True):
        if gen_chunk_flag:
            generator = self.chunk_generator(content)
        else:
            generator = content
        iterator = iter(generator)
        while True:
            batch = list(itertools.islice(iterator, self.batch_size))
            if not batch:
                break
            yield batch


class BatchQueryDocumentProcessor:
    def __init__(
        self,
        self.docsearch: OpenSearchVectorSearch,
        batch_size: int,
    ):
        self.docsearch = docsearch
        self.batch_size = batch_size

    def query_documents(self, s3_path) -> Iterable:
        search_body = {
            "query": {
                # use term-level queries only for fields mapped as keyword
                "match": {"metadata.file_path": s3_path}
            },
            "size": 100000,
            "sort": [{"_score": {"order": "desc"}}],
        }

        query_documents = self.docsearch.client.search(
            index=self.index_name, body=search_body
        )
        document_ids = [doc["_id"] for doc in query_documents["hits"]["hits"]]
        return document_ids

    def batch_generator(self, s3_path):
        generator = self.query_documents(s3_path)
        iterator = iter(generator)
        while True:
            batch = list(itertools.islice(iterator, self.batch_size))
            if not batch:
                break
            yield batch

class OpenSearchIngestionWorker:
    def __init__(
        self,
        docsearch: OpenSearchVectorSearch,
        embeddingModelEndpoint: str,
    ):
        self.docsearch = docsearch
        self.embeddingModelEndpoint = embeddingModelEndpoint

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10)
    )
    def aos_ingestion(self, documents: List[Document]) -> None:

        texts = [doc.page_content for doc in documents]
        metadatas = [doc.metadata for doc in documents]
        embeddings_vectors = self.docsearch.embedding_function.embed_documents(
            list(texts)
        )

        if isinstance(embeddings_vectors[0], dict):
            embeddings_vectors_list = []
            metadata_list = []
            for doc_id, metadata in enumerate(metadatas):
                colbert_vecs = embeddings_vectors[0]["colbert_vecs"][doc_id]
                embeddings_vectors_list.append(
                    embeddings_vectors[0]["dense_vecs"][doc_id]
                )
                metadata.update(
                    {
                        "additional_vecs": {
                            "colbert_vecs": colbert_vecs,
                        }
                    }
                )
                metadata["embedding_endpoint_name"] = self.embeddingModelEndpoint
                metadata_list.append(metadata)
            embeddings_vectors = embeddings_vectors_list
            metadatas = metadata_list
        self.docsearch._OpenSearchVectorSearch__add(
            texts, embeddings_vectors, metadatas=metadatas
        )


class OpenSearchDeleteWorker:
    def __init__(self, docsearch: OpenSearchVectorSearch):
        self.docsearch = docsearch
        self.index_name = self.docsearch.index_name

    def aos_deletion(self, document_ids) -> None:
        document_ids = self.get_document_ids(s3_path)

        bulk_delete_requests = []

        for document_id in document_ids:
            bulk_delete_requests.append(
                {"delete": {"_id": document_id, "_index": self.index_name}}
            )

        self.docsearch.client.bulk(
            index=self.index_name, body=bulk_delete_requests, refresh=True
        )


def update_workspace(workspace_id, embeddingModelEndpoint, index_type):
    (
        embeddings_model_provider,
        embeddings_model_name,
        embeddings_model_dimensions,
        embeddings_model_type,
    ) = get_embedding_info(embeddingModelEndpoint)

    aos_index = workspace_manager.update_workspace_open_search(
        workspace_id,
        embeddingModelEndpoint,
        embeddings_model_provider,
        embeddings_model_name,
        embeddings_model_dimensions,
        embeddings_model_type,
        ["zh"],
        index_type,
    )

    return aos_index


def ingestion_pipeline(s3_files_iterator, batch_chunk_processor, ingestion_worker):
    for file_type, file_content, kwargs in s3_files_iterator:
        try:
            # the res is unified to list[Doucment] type
            res = cb_process_object(s3_client, file_type, file_content, **kwargs)
            for document in res:
                save_content_to_s3(
                    s3_client, document, res_bucket, SplittingType.SEMANTIC.value
                )

            gen_chunk_flag = False if file_type == "csv" else True
            batches = batch_chunk_processor.batch_generator(res, gen_chunk_flag)

            for batch in batches:
                if len(batch) == 0:
                    continue

                for document in batch:
                    if "complete_heading" in document.metadata:
                        document.page_content = (
                            document.metadata["complete_heading"]
                            + " "
                            + document.page_content
                        )
                    else:
                        document.page_content = document.page_content

                    save_content_to_s3(
                        s3_client, document, res_bucket, SplittingType.CHUNK.value
                    )
                ingestion_worker.aos_ingestion(batch)

        except Exception as e:
            logger.error(
                "Error processing object %s: %s",
                kwargs["bucket"] + "/" + kwargs["key"],
                e,
            )
            traceback.print_exc()


def delete_pipeline(s3_files_iterator, document_generator, delete_worker):
    for _, _, kwargs in s3_files_iterator:
        try:
            s3_path = f"s3://{kwargs['bucket']}/{kwargs['key']}"

            batches = document_generator.batch_generator(s3_path)
            for batch in batches:
                if len(batch) == 0:
                    continue
                delete_worker.aos_deletion(batch)

        except Exception as e:
            logger.error(
                "Error processing object %s: %s",
                kwargs["bucket"] + "/" + kwargs["key"],
                e,
            )
            traceback.print_exc()


# Main function to be called by Glue job script
def main():
    logger.info("Starting Glue job with passing arguments: %s", args)
    logger.info("Running in offline mode with consideration for large file size...")

    if index_type == "qq":
        supported_file_types = ["jsonl"]
    elif index_type == "qd":
        supported_file_types = [
            "pdf",
            "txt",
            "doc",
            "md",
            "html",
            "json",
            "csv",
        ]
    else:
        raise ValueError("Invalid index type")

    aos_index_name = update_workspace(workspace_id, embeddingModelEndpoint, index_type)

    file_processor = S3FileProcessor(s3_bucket, s3_prefix, supported_file_types)

    embedding_function = sm_utils.create_embeddings_with_m3_model(
        embeddingModelEndpoint, region
    )
    docsearch = OpenSearchVectorSearch(
        index_name=aos_index_name,
        embedding_function=embedding_function,
        opensearch_url="https://{}".format(aosEndpoint),
        http_auth=awsauth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
    )

    if operation_type == "create":
        s3_files_iterator = file_processor.iterate_s3_files()

        batch_chunk_processor = BatchChunkDocumentProcessor(
            chunk_size=5000, chunk_overlap=30, batch_size=10
        )
        ingestion_worker = OpenSearchIngestionWorker(docsearch, embeddingModelEndpoint)

        ingestion_pipeline(s3_files_iterator, batch_chunk_processor, ingestion_worker)
    elif operation_type == "delete":
        s3_files_iterator = file_processor.iterate_s3_files(extract_content=False)

        batch_query_processor = BatchQueryDocumentProcessor(docsearch, batch_size=10)

        delete_worker = OpenSearchDeleteWorker(docsearch)
        delete_pipeline(s3_files_iterator, batch_query_processor, delete_worker)
    elif operation_type == "update":
        # Delete the documents first
        s3_files_iterator = file_processor.iterate_s3_files(extract_content=False)

        batch_query_processor = BatchQueryDocumentProcessor(docsearch, batch_size=10)

        delete_worker = OpenSearchDeleteWorker(docsearch)
        delete_pipeline(s3_files_iterator, batch_query_processor, delete_worker)

        # Then ingest the documents
        s3_files_iterator = file_processor.iterate_s3_files()

        batch_chunk_processor = BatchChunkDocumentProcessor(
            chunk_size=5000, chunk_overlap=30, batch_size=10
        )
        ingestion_worker = OpenSearchIngestionWorker(docsearch, embeddingModelEndpoint)

        ingestion_pipeline(s3_files_iterator, batch_chunk_processor, ingestion_worker)
    else:
        raise ValueError("Invalid operation type. Valid types: create, delete, update")


if __name__ == "__main__":
    logger.info("boto3 version: %s", boto3.__version__)

    # Set the NLTK data path to the /tmp directory for AWS Glue jobs
    nltk.data.path.append("/tmp")
    # List of NLTK packages to download
    nltk_packages = ["words", "punkt"]
    # Download the required NLTK packages to /tmp
    for package in nltk_packages:
        # Download the package to /tmp/nltk_data
        nltk.download(package, download_dir="/tmp/nltk_data")
    main()
