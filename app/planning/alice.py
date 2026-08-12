from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Mapping
from zoneinfo import ZoneInfo

from app.planning.errors import PlanningIdempotencyInProgressError
from app.planning.events import EventService
from app.planning.models import (
    REMINDER_DELIVERY_JOB_TYPE,
    MutationContext,
    new_uuid4,
    validate_text,
)
from app.planning.parser import Candidate, ParseResult, ParserInput, PlanningParser
from app.planning.parser.normalize import normalize_for_idempotency
from app.planning.tasks import TaskService
from app.planning.repositories import PlanningRepository

if TYPE_CHECKING:
    from app.planning.api.auth import AuthenticatedPlanningContext


ALICE_RESPONSE_KINDS = frozenset({"answer", "confirmation_required", "created", "query_result", "error"})
MAX_SPEECH_LENGTH = 900
MAX_SPOKEN_TITLE_LENGTH = 160
RU_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


@dataclass(frozen=True)
class AliceInterpretRequest:
    """Strict flattened metadata received from a future HA/Yandex adapter."""

    text: str
    reference_time_utc: str
    timezone: str
    locale: str = "ru-RU"
    intent: str | None = None
    dialog: str | None = None
    application_id: str | None = None
    session_id: str | None = None
    message_id: str | None = None
    request_id: str | None = None
    user_id: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class AliceInterpretationResponse:
    response_json: str
    status: int = 200
    replay: bool = False

    @property
    def payload(self) -> dict[str, Any]:
        value = json.loads(self.response_json)
        if not isinstance(value, dict):
            raise RuntimeError("Alice response is not an object")
        return value


def _json_response(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _cap_speech(value: str) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= MAX_SPEECH_LENGTH:
        return normalized
    return normalized[: MAX_SPEECH_LENGTH - 1].rstrip() + "…"


def _spoken_title(value: str) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= MAX_SPOKEN_TITLE_LENGTH:
        return normalized
    return normalized[: MAX_SPOKEN_TITLE_LENGTH - 1].rstrip() + "…"


def _as_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _ru_date(value: str) -> str:
    selected = datetime.strptime(value, "%Y-%m-%d").date()
    return f"{selected.day} {RU_MONTHS[selected.month - 1]} {selected.year} года"


class AliceInterpretationService:
    """Stateless Alice adapter over the shared parser and Planning store."""

    def __init__(
        self,
        database: Any,
        *,
        repository: PlanningRepository | None = None,
        parser: PlanningParser | None = None,
        idempotency_secret: str,
        now_fn: Callable[[], str] | None = None,
    ) -> None:
        validate_text(idempotency_secret, "planning.alice_idempotency_secret", max_length=512)
        self.database = database
        self.repository = repository or PlanningRepository(database, now_fn=now_fn or _utc_now)
        self.task_service = TaskService(database, repository=self.repository, now_fn=now_fn or _utc_now)
        self.event_service = EventService(database, repository=self.repository, now_fn=now_fn or _utc_now)
        self.parser = parser or PlanningParser()
        self.idempotency_secret = idempotency_secret.encode("utf-8")

    def interpret(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        request: AliceInterpretRequest,
    ) -> AliceInterpretationResponse:
        if auth.audience != "ha" or auth.surface != "ha":
            from app.planning.api.errors import PlanningApiError

            raise PlanningApiError(
                code="audience_forbidden",
                message="Only the authenticated HA Alice adapter may use this route.",
                status=403,
            )

        parsed = self.parser.parse(
            ParserInput(
                utterance=request.text,
                reference_time_utc=request.reference_time_utc,
                timezone=request.timezone,
                locale=request.locale,
            )
        )
        if parsed.error_code is not None:
            return self._safe_error(auth, parsed.error_message or "Фразу нельзя безопасно записать.")
        if parsed.candidate is None or parsed.requires_confirmation or parsed.confidence != "high":
            return self._confirmation(auth, parsed)
        if parsed.candidate.operation == "query":
            return self._query(auth, parsed.candidate, request)
        return self._create(auth, parsed, request)

    def _create(
        self,
        auth: AuthenticatedPlanningContext,
        parsed: ParseResult,
        request: AliceInterpretRequest,
    ) -> AliceInterpretationResponse:
        candidate = parsed.candidate
        if candidate is None or candidate.operation != "create":
            raise RuntimeError("Alice create path received a non-create candidate")
        key = self._idempotency_key(request, parsed)
        request_hash = self._request_hash(auth, request)
        with self.database.transaction():
            claim = self.repository.claim_idempotency(
                audience=auth.audience,
                key=key,
                request_hash=request_hash,
            )
            if claim.is_replay:
                assert claim.response_json is not None
                return AliceInterpretationResponse(
                    response_json=claim.response_json,
                    status=claim.response_status or 200,
                    replay=True,
                )
            if not claim.is_new:
                raise PlanningIdempotencyInProgressError(auth.audience, key)

            correlation_id = new_uuid4()
            context = MutationContext(
                audience=auth.audience,
                actor_id=auth.actor_id,
                actor_type=auth.actor_type,
                surface=auth.surface,
                correlation_id=correlation_id,
                source_ref="alice:yandex",
            ).validate()
            fields = dict(candidate.fields)
            if candidate.domain == "reminder":
                object_value = self.repository.create_reminder(
                    title=str(fields["title"]),
                    due_at_utc=str(fields["due_at_utc"]),
                    timezone=str(fields["timezone"]),
                    context=context,
                    outbox_job_type=REMINDER_DELIVERY_JOB_TYPE,
                    outbox_payload={},
                )
            elif candidate.domain == "task":
                object_value = self.task_service.create(
                    title=str(fields["title"]),
                    notes=None,
                    due_date=fields.get("due_date"),
                    due_time=fields.get("due_time"),
                    timezone=fields.get("timezone"),
                    priority=str(fields.get("priority", "none")),
                    project_id=None,
                    context=context,
                )
            elif candidate.domain == "calendar_event":
                object_value = self.event_service.create(
                    title=str(fields["title"]),
                    all_day=bool(fields["all_day"]),
                    timezone=str(fields["timezone"]),
                    context=context,
                    start_at_utc=fields.get("start_at_utc"),
                    end_at_utc=fields.get("end_at_utc"),
                    start_date=fields.get("start_date"),
                    end_date_exclusive=fields.get("end_date_exclusive"),
                    sync_state="local_only",
                )
            else:
                raise RuntimeError("Alice parser returned an unsupported domain")

            object_dict = object_value.to_dict()
            response = self._base_response(
                kind="created",
                speech=self._created_speech(object_dict),
                end_session=True,
                object_value=object_dict,
                correlation_id=correlation_id,
                actor=auth.actor,
            )
            response_json = self.repository.store_idempotency_response(
                audience=auth.audience,
                key=key,
                request_hash=request_hash,
                response=response,
                response_status=200,
                correlation_id=correlation_id,
            )
            return AliceInterpretationResponse(response_json=response_json)

    def _query(
        self,
        auth: AuthenticatedPlanningContext,
        candidate: Candidate,
        request: AliceInterpretRequest,
    ) -> AliceInterpretationResponse:
        fields = dict(candidate.fields)
        query = fields.get("query")
        result: dict[str, Any]
        if query == "tasks_today":
            date_value = str(fields["date"])
            tasks = self.task_service.today(
                reference_time_utc=request.reference_time_utc,
                caller_timezone=request.timezone,
                limit=1001,
                offset=0,
            )
            overdue = self.task_service.overdue(
                reference_time_utc=request.reference_time_utc,
                caller_timezone=request.timezone,
                limit=1001,
                offset=0,
            )
            result = {
                "query": query,
                "date": date_value,
                "items": [item.to_dict() for item in tasks[:3]],
                "total_count": len(tasks),
                "overdue_count": len(overdue),
            }
            speech = self._tasks_speech(tasks, len(overdue), date_value)
        elif query == "active_reminders":
            reminders = [
                item
                for item in self.repository.list_reminders(limit=1001, offset=0)
                if item.status in {"pending", "due"}
            ]
            result = {
                "query": query,
                "items": [item.to_dict() for item in reminders[:3]],
                "total_count": len(reminders),
            }
            speech = self._reminders_speech(reminders, request.timezone)
        elif query == "events_day":
            date_value = str(fields["date"])
            events = self.event_service.query_local_day(
                local_date=date_value,
                caller_timezone=request.timezone,
                limit=1001,
                offset=0,
            )
            result = {
                "query": query,
                "date": date_value,
                "items": [item.to_dict() for item in events[:3]],
                "total_count": len(events),
            }
            speech = self._events_speech(events, date_value, request.timezone)
        else:
            return self._safe_error(auth, "Этот вид запроса пока не поддерживается.")

        response = self._base_response(
            kind="query_result",
            speech=speech,
            end_session=True,
            object_value=result,
            correlation_id=new_uuid4(),
            actor=auth.actor,
        )
        return AliceInterpretationResponse(response_json=_json_response(response))

    def _confirmation(self, auth: AuthenticatedPlanningContext, parsed: ParseResult) -> AliceInterpretationResponse:
        ambiguity = parsed.ambiguities[0] if parsed.ambiguities else None
        if ambiguity is None:
            speech = "Уточните фразу, чтобы я ничего не записала ошибочно."
        elif ambiguity.field == "domain":
            speech = "Уточните: это задача, напоминание или событие?"
        elif ambiguity.field == "end_time":
            proposed = ambiguity.candidates[0] if ambiguity.candidates else "интервал на 60 минут"
            speech = f"У события нет времени окончания. Предлагаю {proposed}. Повторите полную фразу с началом и концом."
        elif ambiguity.field == "recurrence":
            speech = "Повторяющиеся записи пока не поддерживаются. Назовите одну конкретную дату."
        elif ambiguity.field == "date":
            speech = "Уточните конкретный день или дату."
        elif ambiguity.field == "time" and "вечер" in ambiguity.reason:
            speech = "Уточните точное время: значение «вечером» пока не настроено."
        elif ambiguity.field == "time":
            speech = "Уточните точное время и, если нужно, часть суток."
        elif ambiguity.field == "title":
            speech = "Уточните, что именно записать."
        else:
            speech = "Уточните спорную часть фразы, и я повторю запись полностью."
        return AliceInterpretationResponse(
            response_json=_json_response(
                self._base_response(
                    kind="confirmation_required",
                    speech=speech,
                    end_session=False,
                    object_value=None,
                    correlation_id=new_uuid4(),
                    actor=auth.actor,
                )
            )
        )

    def _safe_error(self, auth: AuthenticatedPlanningContext, message: str) -> AliceInterpretationResponse:
        return AliceInterpretationResponse(
            response_json=_json_response(
                self._base_response(
                    kind="error",
                    speech=_cap_speech(message),
                    end_session=True,
                    object_value=None,
                    correlation_id=new_uuid4(),
                    actor=auth.actor,
                )
            )
        )

    @staticmethod
    def _base_response(
        *,
        kind: str,
        speech: str,
        end_session: bool,
        object_value: Mapping[str, Any] | None,
        correlation_id: str,
        actor: Mapping[str, str],
    ) -> dict[str, Any]:
        if kind not in ALICE_RESPONSE_KINDS:
            raise ValueError("unsupported Alice response kind")
        return {
            "schemaVersion": "planning.v1",
            "kind": kind,
            "speech": _cap_speech(speech),
            "end_session": end_session,
            "pending_confirmation_id": None,
            "object": None if object_value is None else dict(object_value),
            "correlation_id": correlation_id,
            "actor": dict(actor),
        }

    def _idempotency_key(self, request: AliceInterpretRequest, parsed: ParseResult | None = None) -> str:
        """Return a private digest of the trusted event identity.

        Yandex ``message_id`` is only unique inside ``session_id``.  A bare
        message ID is intentionally ignored and uses the existing private
        15-second heuristic instead.  ``request_id`` is treated as scoped
        only when an application or session identity is available.
        """

        del parsed  # Kept as a compatibility-shaped private helper argument.
        kind, material = self._event_identity_material(request)
        digest = hmac.new(
            self.idempotency_secret,
            self._canonical_json(material),
            hashlib.sha256,
        ).hexdigest()
        return f"alice:{kind}:{digest}"

    def _event_identity_material(self, request: AliceInterpretRequest) -> tuple[str, dict[str, Any]]:
        if request.message_id and request.session_id:
            return "yandex-message", {
                "kind": "yandex-message",
                "application_id": request.application_id or "",
                "session_id": request.session_id,
                "message_id": request.message_id,
            }

        # request_id has no stronger scope guarantee in the current adapter
        # evidence.  Use it only with an application or session identity.
        # If a message_id was supplied without session_id, deliberately use
        # the fallback path rather than promoting request_id or message_id to
        # a global identity for that delivery.
        if not request.message_id and request.request_id and (request.application_id or request.session_id):
            return "yandex-request", {
                "kind": "yandex-request",
                "application_id": request.application_id or "",
                "session_id": request.session_id or "",
                "request_id": request.request_id,
            }

        reference_epoch = int(_as_datetime(request.reference_time_utc).timestamp())
        return "hmac", {
            "kind": "fallback-15s",
            "application_id": request.application_id or "",
            "session_id": request.session_id or "",
            "normalized_command": normalize_for_idempotency(request.text),
            "timezone": request.timezone,
            "time_bucket_15s": reference_epoch // 15,
        }

    @staticmethod
    def _canonical_json(value: Mapping[str, Any]) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _request_hash(
        self,
        auth: AuthenticatedPlanningContext,
        request: AliceInterpretRequest,
        candidate: Candidate | None = None,
    ) -> str:
        """Hash stable event semantics, not transport arrival or derived times."""

        del candidate  # Candidate timestamps intentionally do not define replay equivalence.
        # The key material includes the fallback bucket or scoped Yandex
        # identity.  Excluding reference_time_utc and the parsed candidate
        # means a retransmitted relative command replays the first response.
        kind, event_identity = self._event_identity_material(request)
        material = {
            "audience": auth.audience,
            "route": "POST /alice/interpret",
            "actor": auth.actor,
            "event_identity_kind": kind,
            "event_identity": event_identity,
            "normalized_command": normalize_for_idempotency(request.text),
            "timezone": request.timezone,
            "locale": request.locale,
            "intent": normalize_for_idempotency(request.intent or ""),
            "dialog": normalize_for_idempotency(request.dialog or ""),
        }
        encoded = self._canonical_json(material)
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    @staticmethod
    def _created_speech(object_value: Mapping[str, Any]) -> str:
        title = _spoken_title(str(object_value.get("title", "запись")))
        domain = object_value.get("domain")
        if domain == "reminder":
            local = _as_datetime(str(object_value["due_at_utc"])).astimezone(ZoneInfo(str(object_value["timezone"])))
            return f"Напоминание «{title}» записано на {_ru_date(local.date().isoformat())} в {local:%H:%M}."
        if domain == "task":
            due_date = object_value.get("due_date")
            due_time = object_value.get("due_time")
            if due_date and due_time:
                return f"Задача «{title}» записана на {_ru_date(str(due_date))} в {due_time}."
            return f"Задача «{title}» записана на {_ru_date(str(due_date))}."
        if bool(object_value.get("all_day")):
            return f"Событие «{title}» записано на весь день {_ru_date(str(object_value['start_date']))}."
        local_start = _as_datetime(str(object_value["start_at_utc"])).astimezone(ZoneInfo(str(object_value["timezone"])))
        local_end = _as_datetime(str(object_value["end_at_utc"])).astimezone(ZoneInfo(str(object_value["timezone"])))
        return f"Событие «{title}» записано {_ru_date(local_start.date().isoformat())} с {local_start:%H:%M} до {local_end:%H:%M}."

    @staticmethod
    def _tasks_speech(tasks: list[Any], overdue_count: int, date_value: str) -> str:
        if not tasks:
            return f"На {_ru_date(date_value)} задач нет. Просроченных: {overdue_count}."
        names = ", ".join(f"«{_spoken_title(item.title)}»" for item in tasks[:3])
        remaining = max(0, len(tasks) - 3)
        suffix = f" Ещё {remaining}." if remaining else ""
        return f"На {_ru_date(date_value)}: {names}.{suffix} Просроченных: {overdue_count}."

    @staticmethod
    def _reminders_speech(reminders: list[Any], timezone_name: str) -> str:
        if not reminders:
            return "Активных напоминаний нет."
        zone = ZoneInfo(timezone_name)
        parts = []
        for item in reminders[:3]:
            local = _as_datetime(item.due_at_utc).astimezone(zone)
            parts.append(f"«{_spoken_title(item.title)}» — {local.day} {RU_MONTHS[local.month - 1]} в {local:%H:%M}")
        remaining = max(0, len(reminders) - 3)
        suffix = f" Ещё {remaining}." if remaining else ""
        return f"Ближайшие напоминания: {', '.join(parts)}.{suffix}"

    @staticmethod
    def _events_speech(events: list[Any], date_value: str, timezone_name: str) -> str:
        if not events:
            return f"На {_ru_date(date_value)} ничего не запланировано."
        zone = ZoneInfo(timezone_name)
        parts = []
        for item in events[:3]:
            title = _spoken_title(item.title)
            if item.all_day:
                parts.append(f"«{title}», весь день")
            else:
                start = _as_datetime(item.start_at_utc).astimezone(zone)
                end = _as_datetime(item.end_at_utc).astimezone(zone)
                parts.append(f"«{title}», {start:%H:%M}–{end:%H:%M}")
        remaining = max(0, len(events) - 3)
        suffix = f" Ещё {remaining}." if remaining else ""
        return f"На {_ru_date(date_value)}: {', '.join(parts)}.{suffix}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
