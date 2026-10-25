"""Versioned API router. Domain routers are included here as phases are completed."""

from fastapi import APIRouter

from app.api.v1.endpoints import (
    admin,
    admin_login,
    admin_ops,
    auth,
    deals,
    disputes,
    group_bot,
    inquiries,
    messages,
    notifications,
    payments,
    telegram,
    users,
    webhook,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(deals.router)
api_router.include_router(telegram.router)
api_router.include_router(admin.router)
api_router.include_router(admin_ops.router)
api_router.include_router(admin_login.router)
api_router.include_router(inquiries.router)
api_router.include_router(messages.router)
api_router.include_router(webhook.router)
api_router.include_router(payments.router)
api_router.include_router(payments.webhook_router)
api_router.include_router(disputes.router)
api_router.include_router(notifications.router)
api_router.include_router(group_bot.router)
