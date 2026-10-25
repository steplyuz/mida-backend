"""Every message the bot sends, and the in-app notification text, in one place.

Templates are plain text (never HTML, so nothing to escape) with named placeholders.
render() raises KeyError when a field is missing, so a half-filled message is never sent.
To add a message: add a kind and a template here, then call render() with that kind.
"""

from __future__ import annotations

from collections.abc import Mapping

DEAL_CREATED = "deal_created"
STATUS_CHANGED = "status_changed"
GROUP_INVITE = "group_invite"
# Group bot messages (posted in the deal's group, never contain personal links).
GROUP_RULES = "group_rules"
GROUP_STATUS = "group_status"
GROUP_HOLAT = "group_holat"
GROUP_HELP = "group_help"
GROUP_HAVOLA_PRIVATE = "group_havola_private"

# `{link_line}` is filled differently for the bot and the web app. The invite link itself is only
# ever sent to the bot; the web app notification just points at the deal page.
TEMPLATES: Mapping[str, str] = {
    DEAL_CREATED: "🆕 Yangi bitim\n{deal_number} · {title}\nSiz bu bitimda {role}siz.",
    STATUS_CHANGED: "🔔 Bitim holati o'zgardi\n{deal_number} · {title}\nHolat: {status}",
    GROUP_INVITE: ("👥 Bitim guruhi tayyor\n{deal_number} · {title}\nGuruh: {chat_title}\n{link_line}"),
    GROUP_RULES: (
        "📋 {deal_number} · {title} bitimi qoidalari\n\n"
        "Shartlar:\n{terms}\n\n"
        "Qoidalar:\n"
        "• To'lov faqat MIDA ko'rsatmasi bo'yicha amalga oshiriladi.\n"
        "• To'lovni o'zingiz xohlagan hisobga emas, faqat ko'rsatilgan hisobga yuboring.\n"
        "• Holatni /holat buyrug'i bilan tekshiring.\n"
        "• Kelishmovchilik bo'lsa, MIDA ilovasi orqali nizo oching."
    ),
    GROUP_STATUS: "🔔 {deal_number} · {title}\nBitim holati: {status}",
    GROUP_HOLAT: "📌 {deal_number} · {title}\nBitim holati: {status}",
    GROUP_HELP: (
        "Buyruqlar:\n"
        "/holat — bitim holati\n"
        "/havola — shaxsiy havolangizni shaxsiy xabarda olish\n"
        "/yordam — shu ro'yxat"
    ),
    GROUP_HAVOLA_PRIVATE: (
        "🔗 {deal_number} · {title}\n"
        "Guruhga qo'shilish uchun sizning shaxsiy bir martalik havolangiz:\n{link}\n"
        "Havola bir marta ishlaydi. Uni boshqalarga yubormang."
    ),
}

INVITE_LINE_BOT = "Sizning shaxsiy bir martalik havolangiz (faqat siz uchun):\n{link}"
INVITE_LINE_APP = "Guruhga qo'shilish havolasi bitim sahifasida."


def render(kind: str, fields: Mapping[str, str], *, link: str | None = None) -> str:
    """Bot text when `link` is given. Without a link, the in-app (web) text is produced."""
    template = TEMPLATES.get(kind)
    if template is None:
        raise KeyError(f"Unknown bot message kind: {kind}")
    values = dict(fields)
    if kind == GROUP_INVITE:
        values["link_line"] = INVITE_LINE_BOT.format(link=link) if link else INVITE_LINE_APP
    return template.format_map(values)
