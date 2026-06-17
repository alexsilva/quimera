"""Memória estruturada e determinística por workspace."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import fcntl


_MAX_NAMESPACE_LEN = 64
_MAX_KEY_LEN = 128
_MAX_VALUE_BYTES = 32_000
_MAX_RESULTS = 100


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class MemorySaveResult:
    revision: int
    namespace: str
    key: str
    updated_at: str


class WorkspaceMemoryStore:
    """Store JSON estruturado por workspace com lock e escrita atômica."""

    def __init__(self, memory_file: Path) -> None:
        self._memory_file = memory_file.expanduser().resolve()
        self._lock_file = self._memory_file.with_suffix(".lock")
        self._memory_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file.parent.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        *,
        namespace: str,
        key: str,
        value: Any,
        ttl_seconds: int | None,
        actor: str | None,
    ) -> MemorySaveResult:
        namespace = self._validate_token(namespace, field_name="namespace", max_len=_MAX_NAMESPACE_LEN)
        key = self._validate_token(key, field_name="key", max_len=_MAX_KEY_LEN)
        normalized_value = self._normalize_value(value)
        serialized_value = json.dumps(normalized_value, ensure_ascii=False, sort_keys=True)
        if len(serialized_value.encode("utf-8")) > _MAX_VALUE_BYTES:
            raise ValueError(
                f"value excede o limite de {_MAX_VALUE_BYTES} bytes serializados"
            )
        normalized_actor = self._normalize_actor(actor)
        tags = self._extract_tags(normalized_value)
        ttl_value = self._normalize_ttl(ttl_seconds)
        now = _utc_now()
        expires_at = _isoformat(now + timedelta(seconds=ttl_value)) if ttl_value is not None else None

        with self._exclusive_session() as data:
            self._prune_expired(data, now)
            entries = data.setdefault("entries", {})
            namespace_entries = entries.setdefault(namespace, {})
            existing = namespace_entries.get(key)
            created_at = existing.get("created_at") if isinstance(existing, dict) else _isoformat(now)
            created_by = existing.get("created_by") if isinstance(existing, dict) else normalized_actor
            namespace_entries[key] = {
                "namespace": namespace,
                "key": key,
                "value": normalized_value,
                "tags": tags,
                "created_at": created_at,
                "created_by": created_by,
                "updated_at": _isoformat(now),
                "updated_by": normalized_actor,
                "ttl_seconds": ttl_value,
                "expires_at": expires_at,
            }
            revision = int(data.get("revision", 0)) + 1
            data["revision"] = revision
            data["updated_at"] = _isoformat(now)
            self._write_data(data)
        return MemorySaveResult(
            revision=revision,
            namespace=namespace,
            key=key,
            updated_at=_isoformat(now),
        )

    def retrieve(
        self,
        *,
        namespace: str | None,
        key: str | None,
        prefix: str | None,
        tags: list[str] | None,
        limit: int | None,
    ) -> dict[str, Any]:
        normalized_namespace = None
        normalized_key = None
        normalized_prefix = None
        if namespace is not None:
            normalized_namespace = self._validate_token(namespace, field_name="namespace", max_len=_MAX_NAMESPACE_LEN)
        if key is not None:
            normalized_key = self._validate_token(key, field_name="key", max_len=_MAX_KEY_LEN)
        if prefix is not None:
            normalized_prefix = self._validate_prefix(prefix)
        normalized_tags = self._normalize_tags(tags or [])
        normalized_limit = self._normalize_limit(limit)
        now = _utc_now()

        with self._exclusive_session() as data:
            changed = self._prune_expired(data, now)
            entries = self._collect_entries(
                data=data,
                namespace=normalized_namespace,
                key=normalized_key,
                prefix=normalized_prefix,
                tags=normalized_tags,
                limit=normalized_limit,
                now=now,
            )
            if changed:
                self._write_data(data)
            revision = int(data.get("revision", 0))
        return {"revision": revision, "entries": entries}

    @contextmanager
    def _exclusive_session(self) -> Iterator[dict[str, Any]]:
        with self._lock_file.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            data = self._read_data()
            yield data
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _read_data(self) -> dict[str, Any]:
        if not self._memory_file.exists():
            return {"revision": 0, "updated_at": None, "entries": {}}
        try:
            data = json.loads(self._memory_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"memory.json inválido: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("memory.json inválido: raiz deve ser objeto")
        entries = data.get("entries")
        if not isinstance(entries, dict):
            data["entries"] = {}
        return data

    def _write_data(self, data: dict[str, Any]) -> None:
        self._memory_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix="memory-",
            suffix=".json.tmp",
            dir=str(self._memory_file.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self._memory_file)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    def _collect_entries(
        self,
        *,
        data: dict[str, Any],
        namespace: str | None,
        key: str | None,
        prefix: str | None,
        tags: list[str],
        limit: int,
        now: datetime,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        entries = data.get("entries", {})
        namespace_names = [namespace] if namespace is not None else sorted(entries.keys())
        for namespace_name in namespace_names:
            namespace_entries = entries.get(namespace_name, {})
            if not isinstance(namespace_entries, dict):
                continue
            for entry_key in sorted(namespace_entries.keys()):
                if key is not None and entry_key != key:
                    continue
                if prefix is not None and not entry_key.startswith(prefix):
                    continue
                raw_entry = namespace_entries.get(entry_key)
                if not isinstance(raw_entry, dict):
                    continue
                raw_tags = self._normalize_tags(raw_entry.get("tags") or [])
                if tags and not set(tags).issubset(set(raw_tags)):
                    continue
                expires_at = _parse_iso(raw_entry.get("expires_at"))
                if expires_at is not None and expires_at <= now:
                    continue
                ttl_remaining = None
                if expires_at is not None:
                    ttl_remaining = max(0, int((expires_at - now).total_seconds()))
                results.append(
                    {
                        "namespace": str(raw_entry.get("namespace") or namespace_name),
                        "key": str(raw_entry.get("key") or entry_key),
                        "value": self._normalize_value(raw_entry.get("value")),
                        "tags": raw_tags,
                        "created_at": raw_entry.get("created_at"),
                        "created_by": raw_entry.get("created_by"),
                        "updated_at": raw_entry.get("updated_at"),
                        "updated_by": raw_entry.get("updated_by"),
                        "ttl_seconds_remaining": ttl_remaining,
                    }
                )
                if len(results) >= limit:
                    return results
        return results

    def _prune_expired(self, data: dict[str, Any], now: datetime) -> bool:
        changed = False
        entries = data.get("entries", {})
        if not isinstance(entries, dict):
            data["entries"] = {}
            return True
        empty_namespaces: list[str] = []
        for namespace_name, namespace_entries in entries.items():
            if not isinstance(namespace_entries, dict):
                empty_namespaces.append(namespace_name)
                changed = True
                continue
            remove_keys: list[str] = []
            for entry_key, raw_entry in namespace_entries.items():
                if not isinstance(raw_entry, dict):
                    remove_keys.append(entry_key)
                    changed = True
                    continue
                expires_at = _parse_iso(raw_entry.get("expires_at"))
                if expires_at is not None and expires_at <= now:
                    remove_keys.append(entry_key)
                    changed = True
            for entry_key in remove_keys:
                namespace_entries.pop(entry_key, None)
            if not namespace_entries:
                empty_namespaces.append(namespace_name)
        for namespace_name in empty_namespaces:
            entries.pop(namespace_name, None)
        return changed

    @staticmethod
    def _normalize_actor(actor: str | None) -> str | None:
        if actor is None:
            return None
        normalized = str(actor).strip()
        return normalized or None

    @staticmethod
    def _normalize_ttl(value: Any) -> int | None:
        if value is None:
            return None
        try:
            ttl = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("ttl_seconds deve ser inteiro positivo") from exc
        if ttl <= 0:
            raise ValueError("ttl_seconds deve ser inteiro positivo")
        return ttl

    @staticmethod
    def _normalize_limit(value: int | None) -> int:
        if value is None:
            return 50
        try:
            limit = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit deve ser inteiro positivo") from exc
        if limit <= 0:
            raise ValueError("limit deve ser inteiro positivo")
        return min(limit, _MAX_RESULTS)

    @staticmethod
    def _normalize_value(value: Any) -> Any:
        try:
            normalized = json.loads(json.dumps(value, ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            raise ValueError("value deve ser JSON-serializable") from exc
        return normalized

    @staticmethod
    def _normalize_tags(values: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in values:
            text = str(raw).strip()
            if not text:
                continue
            if text.startswith("/") or "/" in text or "\\" in text:
                raise ValueError("tags não podem conter path")
            if text not in normalized:
                normalized.append(text)
        return normalized

    @staticmethod
    def _extract_tags(value: Any) -> list[str]:
        if not isinstance(value, dict):
            return []
        tags = value.get("tags")
        if not isinstance(tags, list):
            return []
        return WorkspaceMemoryStore._normalize_tags(tags)

    @staticmethod
    def _validate_token(value: str, *, field_name: str, max_len: int) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError(f"{field_name} não pode ser vazio")
        if len(text) > max_len:
            raise ValueError(f"{field_name} excede o limite de {max_len} caracteres")
        if text.startswith("/") or "/" in text or "\\" in text:
            raise ValueError(f"{field_name} não pode conter path")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
        if any(ch not in allowed for ch in text):
            raise ValueError(
                f"{field_name} contém caracteres inválidos; use apenas letras, números, '.', '_', ':' ou '-'"
            )
        return text

    @staticmethod
    def _validate_prefix(value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("prefix não pode ser vazio")
        if text.startswith("/") or "/" in text or "\\" in text:
            raise ValueError("prefix não pode conter path")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
        if any(ch not in allowed for ch in text):
            raise ValueError(
                "prefix contém caracteres inválidos; use apenas letras, números, '.', '_', ':' ou '-'"
            )
        return text
