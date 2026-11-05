from __future__ import annotations

from typing import Any
import re
import time

import httpx

from ..config import settings
from .logging_service import log_error, log_event


class RAGError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.ragflow_api_key:
        headers["Authorization"] = f"Bearer {settings.ragflow_api_key}"
    return headers


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.ragflow_api_key}"} if settings.ragflow_api_key else {}


def _url(path: str) -> str:
    return f"{settings.ragflow_base_url.rstrip('/')}{path}"


def _use_graphrag(dataset_name: str) -> bool:
    return dataset_name in settings.ragflow_graphrag_datasets


def _parent_child_enabled() -> bool:
    return settings.ragflow_parent_child


def _parser_config(
    use_graphrag: bool = False,
    use_parent_child: bool = True,
    dataset_name: str = "",
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "chunk_token_num": settings.ragflow_chunk_token_num,
        "delimiter": "\n",
        "parent_child": {
            "use_parent_child": use_parent_child,
            "children_delimiter": settings.ragflow_children_delimiter,
        },
        "graphrag": {"use_graphrag": use_graphrag},
    }
    page_ranges = settings.ragflow_page_ranges.get(dataset_name)
    if page_ranges:
        # 跳过封面/公告/前言/目次，避免这些短行变成碎片块
        config["pages"] = page_ranges
    return config


def list_datasets() -> list[dict[str, Any]]:
    if not settings.ragflow_api_key:
        raise RAGError("RAGFLOW_API_KEY 为空，请在 RAGFlow 界面生成 API Key 并填入 .env。")
    try:
        resp = httpx.get(
            _url("/api/v1/datasets"),
            params={"page": 1, "page_size": 100},
            headers=_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise RAGError(f"读取 RAGFlow 数据集失败：{exc}") from exc
    return data.get("data", [])


def find_dataset(name: str) -> dict[str, Any] | None:
    for item in list_datasets():
        if item.get("name") == name:
            return item
    return None


def create_dataset(name: str) -> dict[str, Any]:
    if not settings.ragflow_api_key:
        raise RAGError("RAGFLOW_API_KEY 为空，无法创建 RAGFlow 数据集。")
    try:
        resp = httpx.post(
            _url("/api/v1/datasets"),
            headers=_headers(),
            json={
                "name": name,
                "embedding_model": settings.ragflow_embedding_model,
                "chunk_method": "naive",
                "parser_config": _parser_config(
                    _use_graphrag(name), _parent_child_enabled(), name
                ),
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") not in (0, None):
            raise RAGError(payload.get("message", "创建数据集失败"))
        return payload.get("data", {})
    except RAGError:
        raise
    except Exception as exc:
        raise RAGError(f"创建 RAGFlow 数据集失败：{exc}") from exc


def delete_dataset(dataset_id: str) -> None:
    try:
        resp = httpx.request(
            "DELETE",
            _url("/api/v1/datasets"),
            headers=_headers(),
            json={"ids": [dataset_id]},
            timeout=120,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") not in (0, None):
            raise RAGError(payload.get("message", "删除数据集失败"))
    except RAGError:
        raise
    except Exception as exc:
        raise RAGError(f"删除 RAGFlow 数据集失败：{exc}") from exc


def update_dataset(dataset_id: str, dataset_name: str) -> None:
    try:
        resp = httpx.put(
            _url(f"/api/v1/datasets/{dataset_id}"),
            headers=_headers(),
            json={
                "chunk_method": "naive",
                "parser_config": _parser_config(
                    _use_graphrag(dataset_name),
                    _parent_child_enabled(),
                    dataset_name,
                ),
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") not in (0, None):
            raise RAGError(payload.get("message", "更新数据集失败"))
    except RAGError:
        raise
    except Exception as exc:
        raise RAGError(f"更新 RAGFlow 数据集失败：{exc}") from exc


def upload_text(dataset_id: str, name: str, content: str) -> str:
    try:
        files = {"file": (name, content.encode("utf-8"), "text/plain")}
        resp = httpx.post(
            _url(f"/api/v1/datasets/{dataset_id}/documents"),
            headers=_auth_headers(),
            files=files,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            raise RAGError("上传文档后 RAGFlow 未返回 document_id")
        return str(data[0].get("id", ""))
    except Exception as exc:
        raise RAGError(f"上传 RAGFlow 文档失败：{exc}") from exc


def parse_documents(dataset_id: str, document_ids: list[str]) -> None:
    if not document_ids:
        return
    try:
        resp = httpx.post(
            _url(f"/api/v1/datasets/{dataset_id}/chunks"),
            headers=_headers(),
            json={"document_ids": document_ids},
            timeout=180,
        )
        resp.raise_for_status()
    except Exception as exc:
        raise RAGError(f"解析 RAGFlow 文档失败：{exc}") from exc


def delete_documents(dataset_id: str, document_ids: list[str]) -> None:
    if not document_ids:
        return
    try:
        resp = httpx.request(
            "DELETE",
            _url(f"/api/v1/datasets/{dataset_id}/documents"),
            headers=_headers(),
            json={"ids": document_ids},
            timeout=180,
        )
        resp.raise_for_status()
    except Exception as exc:
        raise RAGError(f"删除 RAGFlow 文档失败：{exc}") from exc


def list_documents(dataset_id: str) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    page = 1
    while page <= 50:
        try:
            resp = httpx.get(
                _url(f"/api/v1/datasets/{dataset_id}/documents"),
                params={"page": page, "page_size": 100},
                headers=_headers(),
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json().get("data") or {}
        except Exception as exc:
            raise RAGError(f"读取 RAGFlow 文档列表失败：{exc}") from exc
        batch = data.get("docs") or []
        docs.extend(batch)
        total = int(data.get("total") or 0)
        if len(batch) < 100 or (total and len(docs) >= total):
            break
        page += 1
    return docs


def document_content(dataset_id: str, document_id: str) -> str:
    """把一篇文档的分片拼回正文（示例库里存的是"问题 + 结构 + SQL"整篇）。"""
    parts: list[str] = []
    page = 1
    while page <= 20:
        resp = httpx.get(
            _url(f"/api/v1/datasets/{dataset_id}/documents/{document_id}/chunks"),
            params={"page": page, "page_size": 100},
            headers=_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        chunks = data.get("chunks") or []
        parts.extend(str(chunk.get("content") or "") for chunk in chunks)
        total = int(data.get("total") or 0)
        if len(chunks) < 100 or (total and len(parts) >= total):
            break
        page += 1
    return "\n".join(part for part in parts if part)


def _vector_weight(question: str) -> float:
    """这次检索里"向量相似度"占的权重（剩下的给关键词）。

    实测（62 篇示例库）：w=0.5 时正确问法只有 0.55、和错项挤在一起；w=0.85~0.9 时
    正确项 0.80~0.84、第二名 0.68~0.71，分差拉得开。所以默认值调到 0.85
    （`ragflow_vector_similarity_weight`），只有"带编号/桩号/设备号这类精确串"的问题
    仍旧偏向关键词（0.35），因为那类串的精确匹配比语义更重要。
    """
    lowered = question.lower()
    keyword_hits = 0
    for pattern in (r"\b\d{3,}\b", r"\b[kK]\s*\d", "错误码", "编号", "设备号", "桩号", "代码"):
        if re.search(pattern, lowered):
            keyword_hits += 1
    semantic = any(word in question for word in ("原则", "规范", "标准", "怎么", "如何", "要求", "方法", "处置", "工艺"))
    if keyword_hits and not semantic:
        return 0.35
    if semantic:
        # 维护规范/处置这类知识问题保持原样：关键词（专业术语）那一路对它们有用
        return 0.7
    return settings.ragflow_vector_similarity_weight


def _post_retrieval(payload: dict[str, Any]) -> list[dict[str, Any]]:
    resp = httpx.post(_url("/api/v1/retrieval"), headers=_headers(), json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json().get("data") or {}
    return data.get("chunks", [])


def retrieve(
    question: str,
    dataset_ids: list[str],
    use_kg: bool = False,
    document_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    started = time.perf_counter()
    payload: dict[str, Any] = {
        "question": question,
        "dataset_ids": dataset_ids,
        "top_k": settings.ragflow_top_k,
        "similarity_threshold": settings.ragflow_similarity_threshold,
        "vector_similarity_weight": _vector_weight(question),
        "page": 1,
        "page_size": settings.ragflow_top_k,
    }
    if use_kg:
        payload["use_kg"] = True
    if document_ids:
        payload["document_ids"] = document_ids

    try:
        chunks = _post_retrieval(payload)
    except Exception as exc:
        if not use_kg:
            log_error("ragflow", "retrieve_failed", exc, dataset_ids=dataset_ids, question=question[:120])
            raise RAGError(f"RAGFlow 检索失败：{exc}") from exc
        chunks = []

    if use_kg and not chunks:
        # 知识图谱尚未构建时 use_kg 会返回空结果，退回普通混合检索。
        log_event("ragflow", "INFO", "use_kg_fallback", dataset_ids=dataset_ids)
        payload.pop("use_kg", None)
        try:
            chunks = _post_retrieval(payload)
        except Exception as exc:
            log_error("ragflow", "retrieve_failed", exc, dataset_ids=dataset_ids, question=question[:120])
            raise RAGError(f"RAGFlow 检索失败：{exc}") from exc

    log_event("ragflow", "INFO", "retrieve_success", dataset_ids=dataset_ids, chunks=len(chunks), duration_ms=round((time.perf_counter() - started) * 1000), question=question[:120])
    return chunks
