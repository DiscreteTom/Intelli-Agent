import json
from typing import Annotated, Any, TypedDict

from common_logic.common_utils.constant import (
    LLMTaskType,
    ToolRuningMode,
    SceneType
)

from common_logic.common_utils.lambda_invoke_utils import (
    invoke_lambda,
    is_running_local,
    node_monitor_wrapper,
    send_trace,
)
from common_logic.common_utils.python_utils import add_messages, update_nest_dict
from common_logic.common_utils.logger_utils import get_logger
from common_logic.common_utils.prompt_utils import get_prompt_templates_from_ddb
from common_logic.common_utils.serialization_utils import JSONEncoder
from functions import get_tool_by_name
from lambda_main.main_utils.parse_config import CommonConfigParser
from langgraph.graph import END, StateGraph
from lambda_main.main_utils.online_entries.agent_base import build_agent_graph,tool_execution

logger = get_logger('common_entry')

class ChatbotState(TypedDict):
    ########### input/output states ###########
    # inputs
    # origianl input question
    query: str 
    # chat history between human and agent
    chat_history: Annotated[list[dict], add_messages] 
    # complete chatbot config, consumed by all the nodes
    chatbot_config: dict  
    # websocket connection id for the agent
    ws_connection_id: str 
    # whether to enbale stream output via ws_connection_id
    stream: bool 
    # message id related to original input question
    message_id: str = None 
    # record running states of different nodes
    trace_infos: Annotated[list[str], add_messages]
    # whether to enbale trace info update via streaming ouput
    enable_trace: bool 
    # outputs
    # final answer generated by whole app graph
    answer: Any  
    # information needed return to user, e.g. intention, context and so on, anything you can get during execution
    extra_response: Annotated[dict, update_nest_dict]

    ########### query rewrite states ###########
    # query rewrite results
    query_rewrite: str = None 

    ########### intention detection states ###########
    # intention type of retrieved intention samples in search engine, e.g. OpenSearch
    intent_type: str = None 
    # retrieved intention samples in search engine, e.g. OpenSearch
    intent_fewshot_examples: list 
    # tools of retrieved intention samples in search engine, e.g. OpenSearch
    intent_fewshot_tools: list

    ########### retriever states ###########
    # contexts information retrieved in search engine, e.g. OpenSearch
    contexts: str = None
    figure: list = None
    
    ########### agent states ###########
    # current output of agent
    agent_current_output: dict
    # record messages during agent tool choose and calling, including agent message, tool ouput and error messages
    agent_tool_history: Annotated[list[dict], add_messages] 
    # the maximum number that agent node can be called
    agent_repeated_call_limit: int 
    # the current call time of agent
    agent_current_call_number: int #
    # whehter the current call time is less than maximum number of agent call
    agent_repeated_call_validation: bool
    # function calling
    # whether the output of agent can be parsed as the valid tool calling
    function_calling_parse_ok: bool
    # whether the current parsed tool calling is run once
    function_calling_is_run_once: bool

####################
# nodes in graph #
####################

@node_monitor_wrapper
def query_preprocess(state: ChatbotState):
    output: str = invoke_lambda(
        event_body=state,
        lambda_name="Online_Query_Preprocess",
        lambda_module_path="lambda_query_preprocess.query_preprocess",
        handler_name="lambda_handler",
    )

    send_trace(f"\n\n**query_rewrite:** \n{output}", state["stream"], state["ws_connection_id"], state["enable_trace"])
    return {"query_rewrite": output}

@node_monitor_wrapper
def intention_detection(state: ChatbotState):
    intent_fewshot_examples = invoke_lambda(
        lambda_module_path="lambda_intention_detection.intention",
        lambda_name="Online_Intention_Detection",
        handler_name="lambda_handler",
        event_body=state,
    )

    intent_fewshot_tools: list[str] = list(
        set([e["intent"] for e in intent_fewshot_examples])
    )

    send_trace(
        f"**intention retrieved:**\n{json.dumps(intent_fewshot_examples,ensure_ascii=False,indent=2)}", state["stream"], state["ws_connection_id"], state["enable_trace"])
    return {
        "intent_fewshot_examples": intent_fewshot_examples,
        "intent_fewshot_tools": intent_fewshot_tools,
        "intent_type": "intention detected",
    }

@node_monitor_wrapper
def llm_rag_results_generation(state: ChatbotState):
    group_name = state['chatbot_config']['group_name']
    llm_config = state["chatbot_config"]["rag_config"]["llm_config"]
    figure_list = state["figure"]
    if figure_list and len(figure_list) > 1:
        figure_list = [figure_list[0]]
    task_type = LLMTaskType.RAG
    prompt_templates_from_ddb = get_prompt_templates_from_ddb(
        group_name,
        model_id = llm_config['model_id'],
    ).get(task_type,{})

    output: str = invoke_lambda(
        lambda_name="Online_LLM_Generate",
        lambda_module_path="lambda_llm_generate.llm_generate",
        handler_name="lambda_handler",
        event_body={
            "llm_config": {
                **prompt_templates_from_ddb,
                **llm_config,
                "stream": state["stream"],
                "intent_type": task_type,
            },
            "llm_input": {
                "contexts": [state["contexts"]],
                "query": state["query"],
                "chat_history": state["chat_history"],
            },
        },
    )
    
    return {
        "answer": output,
        "ddb_additional_kwargs": {
            "figure": figure_list
        }
    }


@node_monitor_wrapper
def agent(state: ChatbotState):
    # two cases to invoke rag function
    # 1. when valid intention fewshot found
    # 2. for the first time, agent decides to give final results
    no_intention_condition = not state['intent_fewshot_examples']
    first_tool_final_response = False
    if (state['agent_current_call_number'] == 1) and state['function_calling_parse_ok'] and state['agent_tool_history']:
        tool_execute_res = state['agent_tool_history'][-1]['additional_kwargs']['raw_tool_call_results'][0]
        tool_name = tool_execute_res['name']
        if tool_name == "give_final_response":
            first_tool_final_response = True

    if no_intention_condition or first_tool_final_response:
        send_trace("no clear intention, switch to rag")
        contexts = knowledge_retrieve(state)['contexts']
        state['contexts'] = contexts
        answer:str = llm_rag_results_generation(state)['answer']
        return {
            "answer": answer,
            "function_calling_is_run_once": True
        }

    # deal with once tool calling
    if state['agent_repeated_call_validation'] and state['function_calling_parse_ok'] and state['agent_tool_history']:
        tool_execute_res = state['agent_tool_history'][-1]['additional_kwargs']['raw_tool_call_results'][0]
        tool_name = tool_execute_res['name']
        output = tool_execute_res['output']
        tool = get_tool_by_name(tool_name,scene=SceneType.COMMON)
        if tool.running_mode == ToolRuningMode.ONCE:
            send_trace("once tool")
            return {
                "answer": str(output['result']),
                "function_calling_is_run_once": True
            }

    response = app_agent.invoke(state)

    return response


@node_monitor_wrapper
def rag_all_index_lambda(state: ChatbotState):
    # Call retriever
    context_list = []
    figure_list = []

    retriever_params = state["chatbot_config"]["rag_config"]["retriever_config"]
    retriever_params["query"] = state["query"]
    output: str = invoke_lambda(
        event_body=retriever_params,
        lambda_name="Online_Function_Retriever",
        lambda_module_path="functions.functions_utils.retriever.retriever",
        handler_name="lambda_handler",
    )

    for doc in output["result"]["docs"]:
        context_list.append(doc["page_content"])
        figure_list = figure_list + doc["figure"]
    
    # Remove duplicate figures
    unique_set = {tuple(d.items()) for d in figure_list}
    unique_figure_list = [dict(t) for t in unique_set]

    return {"contexts": context_list, "figure": unique_figure_list}


knowledge_retrieve = rag_all_index_lambda

@node_monitor_wrapper
def llm_direct_results_generation(state: ChatbotState):
    group_name = state['chatbot_config']['group_name']
    llm_config = state["chatbot_config"]["chat_config"]
    task_type = LLMTaskType.CHAT

    prompt_templates_from_ddb = get_prompt_templates_from_ddb(
        group_name,
        model_id = llm_config['model_id'],
    ).get(task_type,{})
    logger.info(prompt_templates_from_ddb)

    answer: dict = invoke_lambda(
        event_body={
            "llm_config": {
                **llm_config,
                "stream": state["stream"],
                "intent_type": task_type,
                **prompt_templates_from_ddb
            },
            "llm_input": {
                "query": state["query"],
                "chat_history": state["chat_history"],
               
            },
        },
        lambda_name="Online_LLM_Generate",
        lambda_module_path="lambda_llm_generate.llm_generate",
        handler_name="lambda_handler",
    )
    return {"answer": answer}

def final_results_preparation(state: ChatbotState):
    return {"answer": state['answer']}


def matched_query_return(state: ChatbotState):
    return {"answer": state["answer"]}

################
# define edges #
################

def query_route(state: dict):
    return f"{state['chatbot_config']['chatbot_mode']} mode"


def intent_route(state: dict):
    return state["intent_type"]

def agent_route(state: dict):
    if state.get("function_calling_is_run_once",False):
        return "no need tool calling"

    state["agent_repeated_call_validation"] = state['agent_current_call_number'] < state['agent_repeated_call_limit']
    # if state["function_calling_parse_ok"]:
    #     state["function_calling_parsed_tool_name"] = state["function_calling_parsed_tool_calls"][0]["name"]
    # else:
    #     state["function_calling_parsed_tool_name"] = ""

    # if state["agent_repeated_call_validation"] and not state["function_calling_parse_ok"]:
    #     return "invalid tool calling"

    if state["agent_repeated_call_validation"]:
        return "valid tool calling"
        # if state["function_calling_parsed_tool_name"] in ["QA", "service_availability", "explain_abbr"]:
        #     return "force to retrieve all knowledge"
        # elif state["function_calling_parsed_tool_name"] in state["valid_tool_calling_names"]:
        #     return "valid tool calling"
        # else:
        #     return "no need tool calling"
    else:
        # TODO give final strategy
        raise RuntimeError

#############################
# define online top-level graph for app #
#############################

def build_graph(chatbot_state_cls):
    workflow = StateGraph(chatbot_state_cls)
    # add node for all chat/rag/agent mode
    workflow.add_node("query_preprocess", query_preprocess)
    # chat mode
    workflow.add_node("llm_direct_results_generation", llm_direct_results_generation)
    # rag mode
    workflow.add_node("knowledge_retrieve", knowledge_retrieve)
    workflow.add_node("llm_rag_results_generation", llm_rag_results_generation)
    # agent mode
    workflow.add_node("intention_detection", intention_detection)
    workflow.add_node("matched_query_return", matched_query_return)
    # agent sub graph
    workflow.add_node("agent", agent)
    workflow.add_node("tools_execution", tool_execution)
    workflow.add_node("final_results_preparation", final_results_preparation)

    # add all edges
    workflow.set_entry_point("query_preprocess")
    # chat mode
    workflow.add_edge("llm_direct_results_generation", END)
    # rag mode
    workflow.add_edge("knowledge_retrieve", "llm_rag_results_generation")
    workflow.add_edge("llm_rag_results_generation", END)
    # agent mode
    workflow.add_edge("tools_execution", "agent")
    workflow.add_edge("matched_query_return", "final_results_preparation")
    workflow.add_edge("final_results_preparation", END)

    # add conditional edges
    # choose running mode based on user selection:
    # 1. chat mode: let llm generate results directly
    # 2. rag mode: retrive all knowledge and let llm generate results
    # 3. agent mode: let llm generate results based on intention detection, tool calling and retrieved knowledge
    workflow.add_conditional_edges(
        "query_preprocess",
        query_route,
        {
            "chat mode": "llm_direct_results_generation",
            "rag mode": "knowledge_retrieve",
            "agent mode": "intention_detection",
        },
    )

    # three running branch will be chosen based on intention detection results:
    # 1. similar query found: if very similar queries were found in knowledge base, these queries will be given as results
    # 2. intention detected: if intention detected, the agent logic will be invoked
    workflow.add_conditional_edges(
        "intention_detection",
        intent_route,
        {
            "similar query found": "matched_query_return",
            "intention detected": "agent",
        },
    )

    # the results of agent planning will be evaluated and decide next step:
    # 1. valid tool calling: the agent chooses the valid tools, and the tools will be executed
    # 2. no need tool calling: the agent thinks no tool needs to be called, the final results can be generated
    workflow.add_conditional_edges(
        "agent",
        agent_route,
        {
            "valid tool calling": "tools_execution",
            "no need tool calling": "final_results_preparation",
        },
    )

    app = workflow.compile()
    return app

#####################################
# define online sub-graph for agent #
#####################################
app_agent = None
app = None


def common_entry(event_body):
    """
    Entry point for the Lambda function.
    :param event_body: The event body for lambda function.
    return: answer(str)
    """
    global app,app_agent
    if app is None:
        app = build_graph(ChatbotState)
    
    if app_agent is None:
        app_agent = build_agent_graph(ChatbotState)

    # debuging
    if is_running_local():
        with open("common_entry_workflow.png", "wb") as f:
            f.write(app.get_graph().draw_mermaid_png())
        
        with open("common_entry_agent_workflow.png", "wb") as f:
            f.write(app_agent.get_graph().draw_mermaid_png())
            
    ################################################################################
    # prepare inputs and invoke graph
    event_body["chatbot_config"] = CommonConfigParser.from_chatbot_config(
        event_body["chatbot_config"]
    )
    logger.info(f'event_body:\n{json.dumps(event_body,ensure_ascii=False,indent=2,cls=JSONEncoder)}')
    chatbot_config = event_body["chatbot_config"]
    query = event_body["query"]
    use_history = chatbot_config["use_history"]
    chat_history = event_body["chat_history"] if use_history else []
    stream = event_body["stream"]
    message_id = event_body["custom_message_id"]
    ws_connection_id = event_body["ws_connection_id"]
    enable_trace = chatbot_config["enable_trace"]

    # invoke graph and get results
    response = app.invoke(
        {
            "stream": stream,
            "chatbot_config": chatbot_config,
            "query": query,
            "enable_trace": enable_trace,
            "trace_infos": [],
            "message_id": message_id,
            "chat_history": chat_history,
            "agent_tool_history": [],
            "ws_connection_id": ws_connection_id,
            "debug_infos": {},
            "extra_response": {},
            "agent_repeated_call_limit": chatbot_config['agent_repeated_call_limit'],
            "agent_current_call_number": 0,
        }
    )

    return {
        "answer": response["answer"],
        **response["extra_response"],
        "ddb_additional_kwargs": {
            "figure":response.get("figure", [])
        }
    }


main_chain_entry = common_entry
