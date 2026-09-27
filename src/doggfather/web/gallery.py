"""The public gallery at /projects: no login, search and filters."""

from __future__ import annotations

from math import ceil
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request

from ..deps import DB
from ..services import gallery
from .templating import render

router = APIRouter(include_in_schema=False)


@router.get("/projects")
def gallery_page(
    request: Request,
    db: DB,
    q: str = "",
    event: list[str] = Query(default=[]),
    track: list[str] = Query(default=[]),
    tag: list[str] = Query(default=[]),
    sort: str = "recent",
    page: int = 1,
    per: int = 60,
):
    query = gallery.GalleryQuery(q=q, event_ids=event, track_ids=track, tags=tag, sort=sort, page=page,
                                 per_page=per).normalized()
    items, total = gallery.search(db, query)
    pages = max(1, ceil(total / query.per_page))

    def url_with(**changes) -> str:
        params = {"q": query.q, "event": query.event_ids, "track": query.track_ids, "tag": query.tags,
                  "sort": query.sort, "page": 1}
        params.update(changes)
        clean = {k: v for k, v in params.items() if v not in ("", [], None) and not (k == "sort" and v == "recent")
                 and not (k == "page" and v == 1)}
        return "/projects" + ("?" + urlencode(clean, doseq=True) if clean else "")

    return render(request, "projects/gallery.html", {
        "query": query,
        "items": items,
        "total": total,
        "pages": pages,
        "facets": gallery.facets(db, query.event_ids),
        "sorts": gallery.SORT_LABELS,
        "url_with": url_with,
    })
