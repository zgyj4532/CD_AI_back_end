"""Package exports for API v1 endpoints.

This module explicitly imports endpoint submodules so that
`from app.api.v1.endpoints import documents, ...` works
and each submodule's `router` is available for registration.
"""

from . import documents, groups, papers, ai_review, annotations, admin, agent_api

__all__ = [
	"documents",
	"groups",
	"papers",
	"ai_review",
	"annotations",
	"admin",
	"agent_api",
]


