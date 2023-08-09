from typing import Callable, Iterable, List

import pinecone
import streamlit as st
from llama_hub.tools.database.base import DatabaseToolSpec
from llama_index import Document, StorageContext, VectorStoreIndex
from llama_index.agent import OpenAIAgent
from llama_index.tools import QueryEngineTool, ToolMetadata
from llama_index.vector_stores import PineconeVectorStore, SimpleVectorStore
from sqlalchemy import text

from common import Conversation, DatabaseProps, VectorStoreType, get_vector_store_type


class TrackingDatabaseToolSpec(DatabaseToolSpec):
    handler: Callable[[str, Iterable], None]

    def set_handler(self, func: Callable):
        self.handler = func

    def load_data(self, query: str) -> List[Document]:
        """Query and load data from the Database, returning a list of Documents.

        Args:
            query (str): an SQL query to filter tables and rows.

        Returns:
            List[Document]: A list of Document objects.
        """
        documents = []
        with self.sql_database.engine.connect() as connection:
            if query is None:
                raise ValueError("A query parameter is necessary to filter the data")
            else:
                result = connection.execute(text(query))

            items = result.fetchall()

            if self.handler:
                self.handler(query, items)

            for item in items:
                # fetch each item
                doc_str = ", ".join([str(entry) for entry in item])
                documents.append(Document(text=doc_str))
        return documents


@st.cache_resource(show_spinner="Retrieving vector store...")
def get_storage_context(vector_store_id: str):
    vector_store_props = st.session_state.vector_stores[vector_store_id]

    vector_store = None
    match get_vector_store_type(vector_store_props):
        case VectorStoreType.InMemory:
            vector_store = SimpleVectorStore()

        case VectorStoreType.PineconeDB:
            # Initialize connection to pinecone
            pinecone.init(
                api_key=vector_store_props.api_key,
                environment=vector_store_props.environment,
            )

            # Create the index if it does not exist already
            index_name = vector_store_props.index_name
            if index_name not in pinecone.list_indexes():
                pinecone.create_index(index_name, dimension=1536, metric="cosine")

            # Connect to the index
            pinecone_index = pinecone.Index(index_name)
            vector_store = PineconeVectorStore(pinecone_index=pinecone_index)

    # Setup our storage (vector db)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    return storage_context


@st.cache_resource(show_spinner="Connecting to database...")
def get_database_spec(database_id: str) -> TrackingDatabaseToolSpec:
    database: DatabaseProps = st.session_state.databases[database_id]

    db_spec = TrackingDatabaseToolSpec(
        uri=database.uri,
    )

    return db_spec


@st.cache_resource(show_spinner="Generating tools...")
def get_query_tool(vector_store_id: str, database_id: str) -> QueryEngineTool:
    # This function is cached as a resource, so calling it here is fine
    db_spec = get_database_spec(database_id)

    table_list = db_spec.list_tables()

    documents = []
    for table in table_list:
        description = db_spec.describe_tables([table])
        documents.append(Document(text=f'Definition of "{table}" table:\n{description}'))

    # Get the storage context to create a vector index with it
    storage_context = get_storage_context(vector_store_id)
    index = VectorStoreIndex.from_documents(documents=documents, storage_context=storage_context)

    engine = index.as_query_engine()
    query_tool = QueryEngineTool(
        query_engine=engine,
        metadata=ToolMetadata(
            name="table_query_engine",
            description="Contains table descriptions for the database",
        ),
    )

    return query_tool


def database_spec_handler(query, items):
    conversation = st.session_state.conversations[st.session_state.current_conversation]
    conversation.query_results_queue.append((query, items))


@st.cache_resource(show_spinner="Creating agent...")
def get_agent(conversation_id: str, last_update_timestamp: float):
    # Used for invalidating the cache when we want to force create a new agent
    _ = last_update_timestamp

    conversation: Conversation = st.session_state.conversations[conversation_id]

    vector_store_id = conversation.vector_store_id

    tools = []

    # Create tools
    for database_id in conversation.database_ids:
        db_spec = get_database_spec(database_id)
        query_tool = get_query_tool(vector_store_id, database_id)

        # Set a handler that can be called whenever a query is executed
        db_spec.set_handler(database_spec_handler)

        # Add query tool and database tools
        tools.append(query_tool)
        tools += db_spec.to_tool_list()

    # Create the Agent with our tools
    # TODO: save chat history somewhere so we can load it here
    # TODO: remove verbose flag
    agent = OpenAIAgent.from_tools(tools, verbose=True)

    return agent
