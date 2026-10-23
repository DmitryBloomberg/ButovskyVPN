# -*- coding: utf-8 -*-
from fastapi import Request
from .common import render
from ..database import get_active_tariffs

from ..database import get_active_servers


async def render_index(request: Request):
    tariffs = get_active_tariffs()
    servers = get_active_servers()
    return render(request, "index.html", {
        "tariffs": tariffs,
        "servers_count": len(servers),
    })
