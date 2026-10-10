"""演示用 Petstore mock API：配合 examples/petstore-openapi.json 演示网关全链路。

- `/whoami` 无需认证，回显收到的 Authorization 头 → 直观展示网关凭证注入效果；
- `/pets*` 需要 `Authorization: Bearer $DEMO_PETSTORE_TOKEN`（网关注册时配置
  token_env 后会自动注入，LLM/客户端全程看不到真实凭证）。

用法：.venv/bin/python demo/petstore_mock.py [端口]   # 默认 20034
"""

from __future__ import annotations

import os
import sys

import uvicorn
from fastapi import Body, FastAPI, Header, HTTPException

app = FastAPI(title="Petstore Mock (api-to-mcp 演示目标 API)")

TOKEN = os.environ.get("DEMO_PETSTORE_TOKEN", "demo-secret-token")
PETS: dict[int, dict] = {
    1: {"id": 1, "name": "小白", "tag": "dog"},
    2: {"id": 2, "name": "阿咪", "tag": "cat"},
    3: {"id": 3, "name": "旺财", "tag": "dog"},
}
_next_id = 4


def _require_auth(authorization: str | None) -> None:
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="需要 Bearer 认证（应由网关自动注入）")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/whoami")
def whoami(authorization: str | None = Header(None)) -> dict:
    return {
        "authorization": authorization or "(未携带)",
        "note": "本端点无需认证，用于展示网关注入的凭证",
    }


@app.get("/pets")
def list_pets(limit: int = 10, authorization: str | None = Header(None)) -> list[dict]:
    _require_auth(authorization)
    return list(PETS.values())[:limit]


@app.post("/pets")
def create_pet(
    body: dict = Body(...), authorization: str | None = Header(None)
) -> dict:
    _require_auth(authorization)
    global _next_id
    pet = {"id": _next_id, **body}
    PETS[_next_id] = pet
    _next_id += 1
    return pet


@app.get("/pets/{pet_id}")
def get_pet(pet_id: int, authorization: str | None = Header(None)) -> dict:
    _require_auth(authorization)
    if pet_id not in PETS:
        raise HTTPException(status_code=404, detail="pet not found")
    return PETS[pet_id]


@app.delete("/pets/{pet_id}")
def delete_pet(pet_id: int, authorization: str | None = Header(None)) -> dict:
    _require_auth(authorization)
    if pet_id not in PETS:
        raise HTTPException(status_code=404, detail="pet not found")
    del PETS[pet_id]
    return {"deleted": pet_id}


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "20034"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
