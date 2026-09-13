"""Shared admin-facing contract for broadcast audience filters."""

from __future__ import annotations

from typing import Any

from database.db_stats import BROADCAST_FILTER_KEYS, normalize_broadcast_filters


BROADCAST_FILTER_LABELS = {
    'active': 'С активными ключами',
    'inactive': 'Без активных ключей',
    'never_paid': 'Никогда не покупали',
    'expired': 'Ключ истёк',
    'used_trial': 'Брали пробный период',
}


def broadcast_filter_labels(filters: object) -> list[str]:
    """Return selected filter labels in canonical order."""
    return [
        BROADCAST_FILTER_LABELS[key]
        for key in normalize_broadcast_filters(filters)
    ]


def broadcast_filter_summary(filters: object) -> str:
    """Render the selection for an administrator-facing summary."""
    labels = broadcast_filter_labels(filters)
    if not labels:
        return 'не выбраны — все пользователи'
    return ' И '.join(labels)


def broadcast_filter_status(filters: object) -> str:
    """Render the complete administrator-facing filter status line."""
    labels = broadcast_filter_labels(filters)
    if not labels:
        return 'Фильтры не выбраны — все пользователи'
    return 'Фильтры (И): ' + ' И '.join(labels)


def broadcast_audience_state(
    filters: object,
    recipient_count: int,
) -> dict[str, Any]:
    """Build the structured audience state exposed to the contextual editor."""
    selected = normalize_broadcast_filters(filters)
    selected_set = set(selected)
    count = max(0, int(recipient_count))
    return {
        'contract_version': 2,
        'selected_filters': list(selected),
        'available_filters': [
            {
                'key': key,
                'label': BROADCAST_FILTER_LABELS[key],
                'selected': key in selected_set,
            }
            for key in BROADCAST_FILTER_KEYS
        ],
        'logic': 'and',
        'empty_means_all': True,
        'recipient_count': count,
        'launch_blocker': (
            'Нет получателей — рассылка не запустится.'
            if count == 0
            else ''
        ),
    }


__all__ = [
    'BROADCAST_FILTER_LABELS',
    'broadcast_audience_state',
    'broadcast_filter_labels',
    'broadcast_filter_status',
    'broadcast_filter_summary',
]
