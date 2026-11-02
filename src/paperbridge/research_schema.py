"""Strict input types shared by the two MCP registrations."""
from typing import Annotated, Literal

from pydantic import Field

SearchQuery = Annotated[str, Field(strict=True, min_length=1, max_length=1000)]
LibraryQuery = Annotated[str, Field(strict=True, min_length=2, max_length=500)]
ResourceQuery = Annotated[str, Field(strict=True, max_length=500)]
ResourceSubject = Annotated[str, Field(strict=True, min_length=1, max_length=256)]
ResearchLimit = Annotated[int, Field(strict=True, ge=1, le=20)]
ResourceLimit = Annotated[int, Field(strict=True, ge=1, le=50)]
ResearchYear = Annotated[int, Field(strict=True, ge=1000, le=2100)]
ResearchOrder = Literal["relevance", "recent", "citations"]
ResearchKind = Literal["article", "review", "book-chapter", "dissertation", "preprint"]
DiscoverySource = Literal["crossref", "openalex", "combined"]
