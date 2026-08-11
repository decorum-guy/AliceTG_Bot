"""Planning v1 HTTP adapter.

The API package is deliberately an adapter around ``PlanningRepository``.  It
does not parse Alice speech, call Home Assistant services, proxy URLs, or
expose arbitrary repository methods.
"""

from app.planning.api.routes import setup_planning_routes
from app.planning.api.service import PlanningApiService

__all__ = ["PlanningApiService", "setup_planning_routes"]
