from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx

from ..config import settings
from ..store import (
    clear_sync_rows,
    delete_sync_rows,
    list_sync_rows,
    upsert_sync_row,
)
from .rag import (
    RAGError,
    create_dataset,
    delete_documents,
    find_dataset,
    list_datasets,
    list_documents,
    parse_documents,
    update_dataset,
)


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    if suffix in {".md", ".markdown", ".mdx"}:
        return "text/markdown"
    return "text/plain"


def _upload_file(dataset_id: str, path: Path, data: bytes) -> str:
    headers: dict[str, str] = {}
    if settings.ragflow_api_key:
        headers["Authorization"] = f"Bearer {settings.ragflow_api_key}"
    files = {"file": (path.name, data, _mime_type(path))}
    resp = httpx.post(
        f"{settings.ragflow_base_url.rstrip('/')}/api/v1/datasets/{dataset_id}/documents",
        headers=headers,
        files=files,
        timeout=180,
    )
    resp.raise_for_status()
    payload = resp.json().get("data", [])
    if not payload:
        raise RAGError("上传文件未返回 document_id")
    return str(payload[0].get("id", ""))


def sync_dataset(dataset_name: str, kind: str, force: bool = False) -> dict[str, Any]:
    del kind  # 文件类型由 RAGFlow 自带解析器判断，这里只保留目录到数据集的映射。
    dataset = find_dataset(dataset_name)
    if dataset:
        dataset_id = str(dataset.get("id", ""))
        update_dataset(dataset_id, dataset_name)
    else:
        dataset_id = str(create_dataset(dataset_name).get("id", ""))
        clear_sync_rows(dataset_name)
    if not dataset_id:
        raise RAGError(f"数据集 {dataset_name} 缺少 id")

    root = settings.knowledge_root / dataset_name
    local_files: dict[str, Path] = {}
    if root.exists():
        local_files = {
            str(path.relative_to(root)).replace("\\", "/"): path
            for path in root.rglob("*")
            if path.is_file()
        }

    old_rows = {row["relative_path"]: row for row in list_sync_rows(dataset_name)}

    # force=True：忽略内容是否变化，先删除远端文档再全量重传，用于应用新的分块配置。
    if force:
        doc_ids = [
            str(row["ragflow_doc_id"])
            for row in old_rows.values()
            if row.get("ragflow_doc_id")
        ]
        if doc_ids:
            delete_documents(dataset_id, doc_ids)
        clear_sync_rows(dataset_name)
        old_rows = {}

    old_paths = set(old_rows.keys())
    new_paths = set(local_files.keys())

    created = 0
    updated = 0
    deleted = 0
    errors: list[str] = []

    for rel_path in sorted(old_paths - new_paths):
        doc_ids = delete_sync_rows(dataset_name, rel_path)
        try:
            delete_documents(dataset_id, doc_ids)
            deleted += 1
        except RAGError as exc:
            errors.append(f"{rel_path}: {exc}")

    for rel_path, path in sorted(local_files.items()):
        try:
            data = path.read_bytes()
            file_hash = _hash_bytes(data)
            old = old_rows.get(rel_path)
            if old and old["content_hash"] == file_hash:
                continue

            if old:
                delete_documents(dataset_id, [old["ragflow_doc_id"]])
                delete_sync_rows(dataset_name, rel_path)

            doc_id = _upload_file(dataset_id, path, data)
            upsert_sync_row(dataset_name, rel_path, file_hash, 0, doc_id)
            parse_documents(dataset_id, [doc_id])
            if old:
                updated += 1
            else:
                created += 1
        except Exception as exc:
            errors.append(f"{rel_path}: {exc}")

    return {
        "dataset": dataset_name,
        "dataset_id": dataset_id,
        "created": created,
        "updated": updated,
        "deleted": deleted,
        "errors": errors,
    }


def sync_all(force: bool = False) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for dataset_name, kind in settings.knowledge_datasets.items():
        try:
            results.append(sync_dataset(dataset_name, kind, force=force))
        except Exception as exc:
            results.append({"dataset": dataset_name, "error": str(exc)})
    return results


def dataset_status() -> list[dict[str, Any]]:
    try:
        remote = {item.get("name"): item for item in list_datasets()}
        remote_error = ""
    except RAGError as exc:
        remote = {}
        remote_error = str(exc)

    status: list[dict[str, Any]] = []
    for dataset_name in settings.knowledge_datasets:
        rows = list_sync_rows(dataset_name)
        local_count = 0
        root = settings.knowledge_root / dataset_name
        if root.exists():
            local_count = len([p for p in root.rglob("*") if p.is_file()])

        done = 0
        total = 0
        dataset_id = remote.get(dataset_name, {}).get("id")
        if dataset_id:
            try:
                docs = list_documents(str(dataset_id))
                total = len(docs)
                done = sum(1 for doc in docs if str(doc.get("run", "")).upper() == "DONE")
            except RAGError:
                pass

        status.append(
            {
                "name": dataset_name,
                "ragflow_id": dataset_id,
                "local_files": local_count,
                "sync_docs": len(rows),
                "parsed_docs": done,
                "remote_docs": total,
                "exists": dataset_name in remote,
                "error": remote_error,
            }
        )
    return status
