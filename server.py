#!/usr/bin/env python3
"""
RND Viewer — PostgreSQL 전용 FastAPI 서버
ax_dev.rnd_products + ax_dev.rnd_analyses 를 읽어
index.html 이 기대하는 API 형식으로 응답한다.

실행:
  uvicorn server:app --host 0.0.0.0 --port 8792 --reload
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

# ──────────────────────────────────────────────
# 환경 설정
# ──────────────────────────────────────────────
_ENV_FILE = Path(__file__).parent / ".env"


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


_load_env(_ENV_FILE)

PG_HOST = os.environ.get("PG_HOST", "")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_DB   = os.environ.get("PG_DATABASE", "postgres")
PG_USER = os.environ.get("PG_USER", "")
PG_PASS = os.environ.get("PG_PASSWORD", "")
PG_SCHEMA = os.environ.get("PG_SCHEMA", "ax_dev")

STATIC_DIR = Path(__file__).parent

# ──────────────────────────────────────────────
# DB 연결
# ──────────────────────────────────────────────
def _conn():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASS,
        options=f"-c search_path={PG_SCHEMA}",
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def _q(sql: str, params=()) -> list[dict]:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def _jsonify(v):
    """psycopg2 RealDict 의 jsonb 필드를 Python 객체로 변환."""
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return v


# ──────────────────────────────────────────────
# 자사/타사 판별 (DB에 ownership 컬럼 없음 → 브랜드/site로 추론)
# ──────────────────────────────────────────────
_INTERNAL_BRANDS = {"discovery expedition", "f&f", "ff"}

def _ownership(brand: str, site: str = "") -> str:
    """brand 또는 site='internal' 이면 'internal', 아니면 'competitor'."""
    if (brand or "").strip().lower() in _INTERNAL_BRANDS:
        return "internal"
    if (site or "").strip().lower() == "internal":
        return "internal"
    return "competitor"


# ──────────────────────────────────────────────
# 데이터 변환 헬퍼
# ──────────────────────────────────────────────
def _image_assets(image_urls_raw) -> list[dict]:
    """rnd_products.image_urls JSONB → assets 배열 형식."""
    urls = _jsonify(image_urls_raw) or {}
    if isinstance(urls, dict):
        return [
            {"url": v, "public_url": v, "view_name": k, "view": k}
            for k, v in urls.items()
            if v
        ]
    return []


def _build_final_analysis(row: dict) -> dict:
    """rnd_analyses 행 → final_analysis 객체 (index.html 기대 형식)."""
    sections = _visible_sections(_jsonify(row.get("sections_json")) or {})
    ai_labels_raw = _jsonify(row.get("ai_labels_json")) or {}

    # ai_labels: {UML, TL, LSL, SVL, ACL, RL} 형식 보장
    ai_labels = {
        "UML": float(row.get("label_uml") or ai_labels_raw.get("UML") or 0),
        "TL":  float(row.get("label_tl")  or ai_labels_raw.get("TL")  or 0),
        "LSL": float(row.get("label_lsl") or ai_labels_raw.get("LSL") or 0),
        "SVL": float(row.get("label_svl") or ai_labels_raw.get("SVL") or 0),
        "ACL": float(row.get("label_acl") or ai_labels_raw.get("ACL") or 0),
        "RL":  float(row.get("label_rl")  or ai_labels_raw.get("RL")  or 0),
    }

    # manager_brief: sections_json 안에 있으면 그대로 사용
    manager_brief = sections.get("rnd_manager") or sections.get("manager_brief") or {}

    return {
        "product_id":   row.get("product_id"),
        "brand":        row.get("brand", ""),
        "product_name": row.get("product_name", ""),
        "style":        row.get("product_id", ""),
        "summary":      row.get("summary_text", ""),
        "ai_labels":    ai_labels,
        "sections":     sections,
        "manager_brief": manager_brief,
        "schema_version": row.get("schema_version", ""),
        "analysis_mode":  row.get("analysis_mode", ""),
        "agent_count":    row.get("agent_count", 0),
    }


def _visible_sections(sections: dict) -> dict:
    """사용자 화면/API에서는 내부 QC 체크리스트 섹션을 제외한다."""
    if not isinstance(sections, dict):
        return {}
    return {key: value for key, value in sections.items() if key != "quality_checklist"}


def _analysis_to_run(a_row: dict, p_row: dict | None = None) -> dict:
    """rnd_analyses 행 → run 객체 (index.html 기대 형식)."""
    assets = _image_assets((p_row or {}).get("image_urls"))
    return {
        "run_id":          a_row["analysis_id"],
        "product_id":      a_row["product_id"],
        "run_type":        "full_analysis",
        "analysis_status": "complete",
        "created_at":      str(a_row.get("created_at") or ""),
        "updated_at":      str(a_row.get("uploaded_at") or a_row.get("created_at") or ""),
        "final_analysis":  _build_final_analysis(a_row),
        "assets":          assets,
    }


def _product_row_to_api(p: dict, latest_analysis: dict | None) -> dict:
    """rnd_products 행 → products API 형식."""
    run_id  = latest_analysis["analysis_id"] if latest_analysis else None
    updated = str(latest_analysis.get("uploaded_at") or latest_analysis.get("created_at") or "") if latest_analysis else ""
    return {
        "product_id":              p["product_id"],
        "brand":                   p.get("brand", ""),
        "product_name":            p.get("product_name", ""),
        "display_name":            p.get("display_name") or p.get("product_name", ""),
        "color":                   p.get("color", ""),
        "sku":                     p.get("sku", ""),
        "price":                   float(p["price"]) if p.get("price") else None,
        "currency":                p.get("currency", ""),
        "source_url":              p.get("source_url") or p.get("final_url", ""),
        "analysis_count":          1 if latest_analysis else 0,
        "latest_run_id":           run_id,
        "latest_run_updated_at":   updated,
        "analysis_status":         "complete" if latest_analysis else "not_analyzed",
        "representative_image_url": p.get("representative_image_url", ""),
        "image_urls":              _jsonify(p.get("image_urls")) or {},
        "ownership":               _ownership(p.get("brand", ""), p.get("site", "")),
        "ownership_label":         "자사" if _ownership(p.get("brand", ""), p.get("site", "")) == "internal" else "타사",
        "tags":                    [],
        "metadata":                {},
    }


# ──────────────────────────────────────────────
# FastAPI
# ──────────────────────────────────────────────
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")

app = FastAPI(title="RND PG Viewer", docs_url=None, redoc_url=None, root_path=BASE_PATH)
router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    if BASE_PATH:
        inject = f'<script>window.__API_BASE__="{BASE_PATH}";</script>\n'
        html = html.replace("<head>", "<head>\n" + inject, 1)
    return HTMLResponse(html, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
    })


@router.get("/api/status")
async def status() -> dict:
    try:
        _q("SELECT 1")
        pg_ok = True
    except Exception as e:
        pg_ok = False
    return {
        "ok":        pg_ok,
        "mode":      "postgres",
        "can_delete": False,
        "schema":    PG_SCHEMA,
        "host":      PG_HOST,
    }


@router.get("/api/products")
async def products(
    q: str = Query(""),
    brand: str = Query(""),
    status: str = Query(""),
    ownership: str = Query(""),
) -> dict[str, Any]:
    # 제품 목록 + 최신 분석 1건 join
    rows = _q(f"""
        SELECT
            p.*,
            a.analysis_id,
            a.created_at  AS a_created_at,
            a.uploaded_at AS a_uploaded_at
        FROM {PG_SCHEMA}.rnd_products p
        LEFT JOIN LATERAL (
            SELECT analysis_id, created_at, uploaded_at
            FROM {PG_SCHEMA}.rnd_analyses
            WHERE product_id = p.product_id
            ORDER BY COALESCE(uploaded_at, created_at) DESC, agent_count DESC NULLS LAST
            LIMIT 1
        ) a ON true
        ORDER BY p.product_id
    """)

    products_list = []
    for row in rows:
        latest = None
        if row.get("analysis_id"):
            latest = {
                "analysis_id": row["analysis_id"],
                "created_at":  row["a_created_at"],
                "uploaded_at": row["a_uploaded_at"],
            }
        item = _product_row_to_api(row, latest)
        products_list.append(item)

    # 필터링
    if q:
        needle = q.strip().lower()
        products_list = [
            p for p in products_list
            if needle in " ".join([
                str(p.get("brand") or ""),
                str(p.get("product_name") or ""),
                str(p.get("display_name") or ""),
                str(p.get("color") or ""),
                str(p.get("sku") or ""),
            ]).lower()
        ]
    if brand:
        products_list = [p for p in products_list if p.get("brand", "").lower() == brand.lower()]
    if ownership:
        products_list = [p for p in products_list if p.get("ownership", "") == ownership.lower()]

    return {"products": products_list, "mode": "postgres"}


@router.get("/api/products/{product_id}/runs")
async def product_runs(product_id: str) -> dict[str, Any]:
    rows = _q(f"""
        SELECT analysis_id, product_id, created_at, uploaded_at, agent_count
        FROM {PG_SCHEMA}.rnd_analyses
        WHERE product_id = %s
        ORDER BY COALESCE(uploaded_at, created_at) DESC
    """, (product_id,))

    runs = [
        {
            "run_id":          r["analysis_id"],
            "product_id":      r["product_id"],
            "run_type":        "full_analysis",
            "analysis_status": "complete",
            "created_at":      str(r.get("created_at") or ""),
            "updated_at":      str(r.get("uploaded_at") or r.get("created_at") or ""),
        }
        for r in rows
    ]
    return {"runs": runs, "mode": "postgres"}


@router.get("/api/runs/{run_id}")
async def run_detail(run_id: str) -> dict[str, Any]:
    rows = _q(f"""
        SELECT a.*, p.image_urls, p.representative_image_url,
               p.brand, p.product_name, p.color
        FROM {PG_SCHEMA}.rnd_analyses a
        JOIN {PG_SCHEMA}.rnd_products p ON p.product_id = a.product_id
        WHERE a.analysis_id = %s
        LIMIT 1
    """, (run_id,))

    if not rows:
        raise HTTPException(status_code=404, detail="run not found")

    row = rows[0]
    run = _analysis_to_run(row, row)
    return {"run": run, "mode": "postgres"}


app.include_router(router, prefix=BASE_PATH)


# ──────────────────────────────────────────────
# 진입점
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8792, reload=True)
