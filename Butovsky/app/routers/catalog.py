# -*- coding: utf-8 -*-
"""Тарифы и цены — как в боте."""
from fastapi import APIRouter, Request
from .common import render
from ..database import get_active_tariffs, get_active_servers

router = APIRouter()


@router.get("/tariffs")
async def tariffs(request: Request):
    return render(request, "tariffs.html", {
        "tariffs": get_active_tariffs(),
        "servers": get_active_servers(),
    })
