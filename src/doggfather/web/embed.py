"""Embeddable gallery widget.

Two ways to put an event's gallery on another site:

    <iframe src="https://portal.example/embed/events/<slug>" ...></iframe>

or, with automatic height:

    <div data-doggfather-gallery="<slug>"></div>
    <script src="https://portal.example/embed.js" async></script>

``/embed/*`` is the only part of the portal that may be framed by other
origins (``frame-ancestors *``); everything else refuses framing. The widget
shows only public data: submitted projects.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from ..auth import RequiredUser
from ..deps import DB, AppSettings
from ..services import events as event_service
from ..services import gallery
from .organizer import organizer_event
from .templating import render

router = APIRouter(include_in_schema=False)

EMBED_JS = """// Doggfather gallery embed. Usage:
//   <div data-doggfather-gallery="EVENT-SLUG" data-limit="12"></div>
//   <script src="%(origin)s/embed.js" async></script>
(function () {
  "use strict";
  var origin = %(origin_json)s;
  function mount(el) {
    if (el.dataset.doggfatherMounted) return;
    el.dataset.doggfatherMounted = "1";
    var params = new URLSearchParams();
    if (el.dataset.limit) params.set("limit", el.dataset.limit);
    if (el.dataset.track) params.set("track", el.dataset.track);
    var frame = document.createElement("iframe");
    frame.src = origin + "/embed/events/" + encodeURIComponent(el.dataset.doggfatherGallery) + "?" + params;
    frame.title = "Hackathon gallery";
    frame.loading = "lazy";
    frame.style.cssText = "width:100%%;border:0;min-height:320px;display:block;background:#0B1020";
    el.appendChild(frame);
    window.addEventListener("message", function (e) {
      if (e.origin !== origin || !e.data || e.data.doggfather !== "resize" || e.source !== frame.contentWindow) return;
      frame.style.height = Math.min(Math.max(e.data.height, 200), 20000) + "px";
    });
  }
  function scan() { document.querySelectorAll("[data-doggfather-gallery]").forEach(mount); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", scan); else scan();
})();
"""


@router.get("/embed/events/{slug}")
def embed_gallery(request: Request, db: DB, slug: str, limit: int = Query(12, ge=1, le=60), track: str = ""):
    event = event_service.get_event_by_slug(db, slug)
    query = gallery.GalleryQuery(event_ids=[event.id], track_ids=[track] if track else [], per_page=limit, sort="recent")
    items, total = gallery.search(db, query)
    return render(request, "embed/gallery.html", {"event": event, "items": items, "total": total})


@router.get("/embed.js")
def embed_script(settings: AppSettings):
    body = EMBED_JS % {"origin": settings.base_url, "origin_json": json.dumps(settings.base_url)}
    return Response(body, media_type="application/javascript; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"})


@router.get("/organize/{slug}/embed")
def embed_page(request: Request, db: DB, user: RequiredUser, slug: str):
    event = organizer_event(db, user, slug)
    return render(request, "organize/embed.html", {"event": event, "tab": "embed"})
