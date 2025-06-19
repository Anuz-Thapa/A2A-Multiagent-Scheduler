import asyncio
import json
import uuid
from datetime import datetime
from typing import Any, AsyncIterable, List

import httpx
import nest_asyncio
from a2a.client import A2ACardResolver
from a2a.types import (
    AgentCard,
    MessageSendParams,
    SendMessageRequest,
    SendMessageResponse,
    SendMessageSuccessResponse,
    Task,
)
from dotenv import load_dotenv
from google.adk import Agent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.artifacts import InMemoryArtifactService
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from google.adk.models.lite_llm import LiteLlm
from .pickleball_tools import (
    book_pickleball_court,
    list_court_availabilities,
)
from .remote_agent_connection import RemoteAgentConnections

# load_dotenv()
nest_asyncio.apply()


class HostAgent:
    """The Host agent."""

    def __init__(
        self,
    ):
        self.remote_agent_connections: dict[str, RemoteAgentConnections] = {}  #dictionary of remote agents connection
        self.cards: dict[str, AgentCard] = {}
        self.agents: str = ""
        self._agent = self.create_agent()  #private attribute
        self._user_id = "host_agent"  #private attribute
        self._runner = Runner(
            app_name=self._agent.name,
            agent=self._agent,
            artifact_service=InMemoryArtifactService(),
            session_service=InMemorySessionService(),
            memory_service=InMemoryMemoryService(),
        )

    async def _async_init_components(self, remote_agent_addresses: List[str]):  #receive remote agent address as a parameter.
        async with httpx.AsyncClient(timeout=30) as client:
            for address in remote_agent_addresses:
                card_resolver = A2ACardResolver(client, address)
                try:
                    card = await card_resolver.get_agent_card()  # get agent card 


                    remote_connection = RemoteAgentConnections(   #remote_connection is the instance of the RemoteAgentConnections class 
                        agent_card=card, agent_url=address    # establish connection using agent card and address
                    )


                    self.remote_agent_connections[card.name] = remote_connection   # add the remote connection object or instance to the dictionary with card name as a key
                    self.cards[card.name] = card  # also add the card to the card dictionary.


                except httpx.ConnectError as e:
                    print(f"ERROR: Failed to get agent card from {address}: {e}")
                except Exception as e:
                    print(f"ERROR: Failed to initialize connection for {address}: {e}")

        agent_info = [
            json.dumps({"name": card.name, "description": card.description})
            for card in self.cards.values()
        ]
        print("agent_info:", agent_info)
        self.agents = "\n".join(agent_info) if agent_info else "No friends found"   #add agent info to agents string.

    @classmethod   #this classmethod decorator allow the function to be called directly using the classname,cls is used instead of self,cannot modify class attribute
    async def create(   #this method belogs to the class itself not the instance.
        cls,     #cls references to the HostAgent class
        remote_agent_addresses: List[str],
    ):
        instance = cls()    #this instantiate the HostAgent class when create function will be called by Hostagent class,so this line also at the same time calls the __init__ constructor which contain agent creating function.
        await instance._async_init_components(remote_agent_addresses)   #also call _async_init_components function through instance since it is not a @classmethod.
        return instance    # and return the instance or object.

    def create_agent(self) -> Agent:
        return Agent(
            # model="gemini-2.5-flash-preview-04-17",
            model=LiteLlm(model="ollama_chat/llama3.2:3b"),
            name="Host_Agent",
            instruction=self.root_instruction,
            description="This Host agent orchestrates scheduling pickleball with friends.",
            tools=[
                self.send_message,
                book_pickleball_court,
                list_court_availabilities,
            ],
        )

    def root_instruction(self, context: ReadonlyContext) -> str:    #based on this instruction the agent call the send_message tool and using user query extract the important detail and send the message request to remote agent.
        return f"""
        **Role:** You are the Host Agent, an expert scheduler for pickleball games. Your primary function is to coordinate with friend agents to find a suitable time to play and then book a court.

        **Core Directives:**

        *   **Initiate Planning:** When asked to schedule a game, first determine who to invite and the desired date range from the user.
        *   **Task Delegation:** Use the `send_message` tool to ask each friend for their availability.
            *   Frame your request clearly (e.g., "Are you available for pickleball between 2024-08-01 and 2024-08-03?").
            *   Make sure you pass in the official name of the friend agent for each message request.
        *   **Analyze Responses:** Once you have availability from all friends, analyze the responses to find common timeslots.
        *   **Check Court Availability:** Before proposing times to the user, use the `list_court_availabilities` tool to ensure the court is also free at the common timeslots.
        *   **Propose and Confirm:** Present the common, court-available timeslots to the user for confirmation.
        *   **Book the Court:** After the user confirms a time, use the `book_pickleball_court` tool to make the reservation. This tool requires a `start_time` and an `end_time`.
        *   **Transparent Communication:** Relay the final booking confirmation, including the booking ID, to the user. Do not ask for permission before contacting friend agents.
        *   **Tool Reliance:** Strictly rely on available tools to address user requests. Do not generate responses based on assumptions.
        *   **Readability:** Make sure to respond in a concise and easy to read format (bullet points are good).
        *   Each available agent represents a friend. So Bob_Agent represents Bob.
        *   When asked for which friends are available, you should return the names of the available friends (aka the agents that are active).
        *   When get

        **Today's Date (YYYY-MM-DD):** {datetime.now().strftime("%Y-%m-%d")}

        <Available Agents>
        {self.agents}
        </Available Agents>
        """

    async def stream(    #this function receive and give response to the user query. triggered automatically when user provide prompt in adk ui.
        self, query: str, session_id: str
    ) -> AsyncIterable[dict[str, Any]]:
        """
        Streams the agent's response to a given query.
        """
        session = await self._runner.session_service.get_session(   #get the session
            app_name=self._agent.name,
            user_id=self._user_id,
            session_id=session_id,
        )
        content = types.Content(role="user", parts=[types.Part.from_text(text=query)])
        if session is None:
            session = await self._runner.session_service.create_session(    #if session is not None create a new session.
                app_name=self._agent.name,
                user_id=self._user_id,
                state={},
                session_id=session_id,
            )
        async for event in self._runner.run_async(     #on running runner.run_async give the response
            user_id=self._user_id, session_id=session.id, new_message=content
        ):
            if event.is_final_response():
                response = ""
                if (
                    event.content
                    and event.content.parts
                    and event.content.parts[0].text
                ):
                    response = "\n".join(
                        [p.text for p in event.content.parts if p.text]
                    )
                yield {
                    "is_task_complete": True,
                    "content": response,
                }
            else:
                yield {
                    "is_task_complete": False,
                    "updates": "The host agent is thinking...",
                }

    async def send_message(self, agent_name: str, task: str, tool_context: ToolContext):     #this function is a tool passed to the Hostagent that use this function tool to send the message to the other agent to send the message
        """Sends a task to a remote friend agent."""
        if agent_name not in self.remote_agent_connections:  #since remote agent connections is the dict containing agent name as key and connection (client) as value
            raise ValueError(f"Agent {agent_name} not found")
        client = self.remote_agent_connections[agent_name]    #get the remote agent connection object or instance  for agent_name 

        if not client:
            raise ValueError(f"Client not available for {agent_name}")
                                                   
        # Simplified task and context ID management
        state = tool_context.state
        task_id = state.get("task_id", str(uuid.uuid4()))   #generates the random unique id.
        context_id = state.get("context_id", str(uuid.uuid4()))
        message_id = str(uuid.uuid4())
    #  in the payload to be send to the particular agent,all these ids are send.
        payload = {
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": task}], 
                "messageId": message_id,
                "taskId": task_id,
                "contextId": context_id,
            },
        }

        message_request = SendMessageRequest(
            id=message_id, params=MessageSendParams.model_validate(payload)
        )
        send_response: SendMessageResponse = await client.send_message(message_request)   #send message request to the remote agent calling the send_message function in remote_agent_connection class through its instance client.
        print("send_response", send_response)

        if not isinstance(
            send_response.root, SendMessageSuccessResponse
        ) or not isinstance(send_response.root.result, Task):
            print("Received a non-success or non-task response. Cannot proceed.")
            return

        response_content = send_response.root.model_dump_json(exclude_none=True)
        json_content = json.loads(response_content)

        resp = []
        if json_content.get("result", {}).get("artifacts"):
            for artifact in json_content["result"]["artifacts"]:
                if artifact.get("parts"):
                    resp.extend(artifact["parts"])
        return resp


def _get_initialized_host_agent_sync():
    """Synchronously creates and initializes the HostAgent."""

    async def _async_main():
        # Hardcoded URLs for the friend agents
        friend_agent_urls = [
            "http://localhost:10002",  # Karley's Agent
            "http://localhost:10003",  # Nate's Agent
            "http://localhost:10004",  # Kaitlynn's Agent
        ]

        print("initializing host agent")
        hosting_agent_instance = await HostAgent.create(   # since create is the classmethod,it can be called directly using HostAgent class
            remote_agent_addresses=friend_agent_urls   #this function call also returns instance of HostAgent class which later call create_agent
        )
        print("HostAgent initialized")
        return hosting_agent_instance.create_agent()    

    try:
        return asyncio.run(_async_main())    #running _async_main function.
    except RuntimeError as e:
        if "asyncio.run() cannot be called from a running event loop" in str(e):
            print(
                f"Warning: Could not initialize HostAgent with asyncio.run(): {e}. "
                "This can happen if an event loop is already running (e.g., in Jupyter). "
                "Consider initializing HostAgent within an async function in your application."
            )
        else:
            raise


root_agent = _get_initialized_host_agent_sync()
















