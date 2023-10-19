from typing import Any, List, Optional, Sequence

from llama_index.bridge.pydantic import BaseModel, Field
from llama_index.embeddings.utils import resolve_embed_model
from llama_index.indices.service_context import ServiceContext
from llama_index.ingestion.client import (
    ConfigurableDataSinkNames,
    ConfigurableDataSourceNames,
    ConfigurableTransformationNames,
    ConfiguredTransformationItem,
    DataSinkCreate,
    DataSourceCreate,
    PipelineCreate,
)
from llama_index.ingestion.client.client import PlatformApi
from llama_index.ingestion.data_sinks import ConfiguredDataSink
from llama_index.ingestion.data_sources import ConfiguredDataSource
from llama_index.ingestion.transformations import ConfiguredTransformation
from llama_index.node_parser import SentenceAwareNodeParser
from llama_index.readers.base import ReaderConfig
from llama_index.schema import BaseNode, Document, TransformComponent
from llama_index.vector_stores.types import BasePydanticVectorStore

DEFAULT_PIPELINE_NAME = "pipeline"
DEFAULT_PROJECT_NAME = "project"
BASE_URL = "http://localhost:8000"


def run_transformations(
    nodes: List[BaseNode],
    transformations: Sequence[TransformComponent],
    in_place: bool = True,
    **kwargs: Any,
) -> List[BaseNode]:
    """Run a series of transformations on a set of nodes.

    Args:
        nodes: The nodes to transform.
        transformations: The transformations to apply to the nodes.

    Returns:
        The transformed nodes.
    """
    if not in_place:
        nodes = list(nodes)

    for transform in transformations:
        nodes = transform(nodes, **kwargs)

    return nodes


async def arun_transformations(
    nodes: List[BaseNode],
    transformations: Sequence[TransformComponent],
    in_place: bool = True,
    **kwargs: Any,
) -> List[BaseNode]:
    """Run a series of transformations on a set of nodes.

    Args:
        nodes: The nodes to transform.
        transformations: The transformations to apply to the nodes.

    Returns:
        The transformed nodes.
    """
    if not in_place:
        nodes = list(nodes)

    for transform in transformations:
        nodes = await transform.acall(nodes, **kwargs)

    return nodes


class IngestionPipeline(BaseModel):
    """An ingestion pipeline that can be applied to data."""

    name: str = Field(description="Unique name of the ingestion pipeline")
    base_url: str = Field(default=BASE_URL, description="Base URL for the platform")

    configured_transformations: List[ConfiguredTransformation] = Field(
        description="Serialized schemas of transformations to apply to the data"
    )

    transformations: List[TransformComponent] = Field(
        description="Transformations to apply to the data"
    )

    documents: Optional[Sequence[Document]] = Field(description="Documents to ingest")
    reader: Optional[ReaderConfig] = Field(description="Reader to use to read the data")
    vector_store: Optional[BasePydanticVectorStore] = Field(
        description="Vector store to use to store the data"
    )

    def __init__(
        self,
        name: str = DEFAULT_PIPELINE_NAME,
        transformations: Optional[List[TransformComponent]] = None,
        reader: Optional[ReaderConfig] = None,
        documents: Optional[Sequence[Document]] = None,
        vector_store: Optional[BasePydanticVectorStore] = None,
        base_url: str = BASE_URL,
    ) -> None:
        if documents is None and reader is None:
            raise ValueError("Must provide either documents or a reader")

        if transformations is None:
            transformations = self._get_default_transformations()

        configured_transformations: List[ConfiguredTransformation] = []
        for transformation in transformations:
            configured_transformations.append(
                ConfiguredTransformation.from_component(transformation)
            )

        super().__init__(
            name=name,
            configured_transformations=configured_transformations,
            transformations=transformations,
            reader=reader,
            documents=documents,
            vector_store=vector_store,
            base_url=base_url,
        )

    @classmethod
    def from_service_context(
        cls,
        service_context: ServiceContext,
        name: str = DEFAULT_PIPELINE_NAME,
        reader: Optional[ReaderConfig] = None,
        documents: Optional[Sequence[Document]] = None,
        vector_store: Optional[BasePydanticVectorStore] = None,
    ) -> "IngestionPipeline":
        transformations = [
            *service_context.transformations,
            service_context.embed_model,
        ]

        return cls(
            name=name,
            transformations=transformations,
            reader=reader,
            documents=documents,
            vector_store=vector_store,
        )

    def _get_default_transformations(self) -> List[TransformComponent]:
        return [
            SentenceAwareNodeParser(),
            resolve_embed_model("default"),
        ]

    def register(
        self, project_name: str = DEFAULT_PROJECT_NAME, verbose: bool = True
    ) -> str:
        client = PlatformApi(base_url=BASE_URL)

        configured_transformations: List[ConfiguredTransformationItem] = []
        for item in self.configured_transformations:
            name = ConfigurableTransformationNames[
                item.configurable_transformation_type.name
            ]
            configured_transformations.append(
                ConfiguredTransformationItem(
                    transformation_name=name,
                    component=item.component,
                    configurable_transformation_type=item.configurable_transformation_type.name,
                )
            )

            # remove callback manager
            configured_transformations[-1].component.pop("callback_manager", None)  # type: ignore

        data_sinks = []
        if self.vector_store is not None:
            configured_data_sink = ConfiguredDataSink.from_component(self.vector_store)
            sink_type = ConfigurableDataSinkNames[
                configured_data_sink.configurable_data_sink_type.name
            ]
            data_sinks.append(
                DataSinkCreate(
                    name=configured_data_sink.name,
                    sink_type=sink_type,
                    component=configured_data_sink.component,
                )
            )

        data_sources = []
        if self.reader is not None:
            if self.reader.reader.is_remote:
                configured_data_source = ConfiguredDataSource.from_component(
                    self.reader
                )
                source_type = ConfigurableDataSourceNames[
                    configured_data_source.configurable_data_source_type.name
                ]
                data_sources.append(
                    DataSourceCreate(
                        name=configured_data_source.name,
                        source_type=source_type,
                        component=configured_data_source.component,
                    )
                )
            else:
                documents = self.reader.read()
                if self.documents is not None:
                    documents += self.documents
                else:
                    self.documents = documents

        if self.documents is not None:
            for document in self.documents:
                configured_data_source = ConfiguredDataSource.from_component(document)
                source_type = ConfigurableDataSourceNames[
                    configured_data_source.configurable_data_source_type.name
                ]
                data_sources.append(
                    DataSourceCreate(
                        name=configured_data_source.name,
                        source_type=source_type,
                        component=document,
                    )
                )

        project = client.project.create_project_api_project_post(name=project_name)
        assert project.id is not None, "Project ID should not be None"

        # upload
        pipeline = client.project.upsert_pipeline_for_project(
            project.id,
            request=PipelineCreate(
                name=self.name,
                configured_transformations=configured_transformations,
                data_sinks=data_sinks,
                data_sources=data_sources,
            ),
        )
        assert pipeline.id is not None, "Pipeline ID should not be None"

        # Print playground URL if not running remote
        if verbose:
            print(
                "Pipeline available at: https://llamalink.llamaindex.ai/"
                f"playground?id={pipeline.id}"
            )

        return pipeline.id

    def run_remote(self, project_name: str = DEFAULT_PROJECT_NAME) -> str:
        client = PlatformApi(base_url=BASE_URL)

        pipeline_id = self.register(project_name=project_name, verbose=False)

        # start pipeline?
        # the `PipeLineExecution` object should likely generate a URL at some point
        pipeline_execution = client.pipeline.create_configured_transformation_execution(
            pipeline_id
        )

        assert (
            pipeline_execution.id is not None
        ), "Pipeline execution ID should not be None"

        print(
            "Find your remote results here: https://llamalink.llamaindex.ai/"
            f"pipelines/execution?id={pipeline_execution.id}"
        )

        return pipeline_execution.id

    def run_local(
        self, show_progress: bool = False, **kwargs: Any
    ) -> Sequence[BaseNode]:
        input_nodes: List[BaseNode] = []
        if self.documents is not None:
            input_nodes += self.documents

        if self.reader is not None:
            input_nodes += self.reader.read()

        nodes = run_transformations(
            input_nodes,
            self.transformations,
            show_progress=show_progress,
            **kwargs,
        )

        if self.vector_store is not None:
            self.vector_store.add([n for n in nodes if n.embedding is not None])

        return nodes