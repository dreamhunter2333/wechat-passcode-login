"""微信公众号 关注+验证码登录服务"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth_store import AuthStore
from .config import ROOT, get_settings, init_db
from .crypto import CryptError, WXMsgCrypt
from .session_store import SessionStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

settings = get_settings()
init_db()

sessions = SessionStore(ttl=settings.session_ttl, max_fails=settings.max_fails)
auth = AuthStore(ttl=settings.auth_ttl)
crypto: Optional[WXMsgCrypt] = None
if settings.wechat_aes_key:
    try:
        crypto = WXMsgCrypt(settings.wechat_token, settings.wechat_aes_key, settings.wechat_app_id)
        logger.info("已启用安全模式 AES")
    except Exception as e:
        logger.error(f"AES 初始化失败: {e}")

app = FastAPI(title="WeChat Login")
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


class LoginStartResp(BaseModel):
    session_id: str
    code: str
    ttl: int


class LoginStatusReq(BaseModel):
    session_id: str


class LoginStatusResp(BaseModel):
    status: str  # pending | scanned | expired


class MeResp(BaseModel):
    openid: str
    expires_at: float


def _check_signature(signature: str, timestamp: str, nonce: str) -> bool:
    parts = sorted([settings.wechat_token, timestamp, nonce])
    expected = hashlib.sha1("".join(parts).encode()).hexdigest()
    return hmac.compare_digest(expected, signature)


def _build_reply_xml(to_user: str, from_user: str, content: str) -> str:
    root = ET.Element("xml")
    for tag, text in (
        ("ToUserName", to_user),
        ("FromUserName", from_user),
        ("CreateTime", str(int(time.time()))),
        ("MsgType", "text"),
        ("Content", content),
    ):
        el = ET.SubElement(root, tag)
        el.text = text
    return ET.tostring(root, encoding="unicode")


def _reply(plain_xml: str, params) -> Response:
    if crypto:
        envelope, *_ = crypto.encrypt(plain_xml, params.get("timestamp"), params.get("nonce"))
        return Response(envelope, media_type="application/xml")
    return Response(plain_xml, media_type="application/xml")


def _set_auth_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(
        settings.cookie_name,
        token,
        max_age=settings.auth_ttl,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/",
    )


@app.get("/login/start", response_model=LoginStartResp)
def login_start():
    s = sessions.create()
    return LoginStartResp(
        session_id=s.session_id,
        code=s.code,
        ttl=settings.session_ttl,
    )


@app.post("/login/status", response_model=LoginStatusResp)
def login_status(req: LoginStatusReq, response: Response):
    s = sessions.get(req.session_id)
    if not s:
        return LoginStatusResp(status="expired")
    if s.status == "scanned" and s.openid:
        # 幂等签发：先把 session 翻成 issued，成功才创建 token + 撤销同 openid 旧 token
        if sessions.mark_issued(s.session_id):
            auth.revoke_openid(s.openid)
            token, _ = auth.create(s.openid)
            _set_auth_cookie(response, token)
        return LoginStatusResp(status="scanned")
    return LoginStatusResp(status=s.status)


@app.get("/me", response_model=MeResp)
def me(response: Response, auth_token: str = Cookie(default="", alias="auth_token")):
    new_exp = auth.touch(auth_token)
    if new_exp is None:
        raise HTTPException(401, "not logged in")
    a = auth.get(auth_token)
    if not a:
        raise HTTPException(401, "not logged in")
    _set_auth_cookie(response, auth_token)  # 浏览器端同步续期
    return MeResp(openid=a.openid, expires_at=new_exp)


@app.post("/logout")
def logout(response: Response, auth_token: str = Cookie(default="", alias="auth_token")):
    auth.revoke(auth_token)
    response.delete_cookie(settings.cookie_name, path="/")
    return {"ok": True}


@app.get("/wechat")
def verify(signature: str = "", timestamp: str = "", nonce: str = "", echostr: str = ""):
    if not _check_signature(signature, timestamp, nonce):
        raise HTTPException(403, "signature mismatch")
    return PlainTextResponse(echostr)


@app.post("/wechat")
async def callback(request: Request):
    params = request.query_params
    if not _check_signature(params.get("signature", ""), params.get("timestamp", ""), params.get("nonce", "")):
        raise HTTPException(403, "signature mismatch")

    body = await request.body()
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return PlainTextResponse("success")

    encrypt_node = root.findtext("Encrypt")
    if encrypt_node:
        if not crypto:
            logger.error("收到加密消息但未配置 AES")
            return PlainTextResponse("success")
        try:
            plain = crypto.decrypt(
                encrypt_node,
                params.get("msg_signature", ""),
                params.get("timestamp", ""),
                params.get("nonce", ""),
            )
            root = ET.fromstring(plain)
        except CryptError as e:
            logger.error(f"解密失败: {e}")
            raise HTTPException(403, "decrypt failed")

    msg_type = (root.findtext("MsgType") or "").lower()
    openid = root.findtext("FromUserName") or ""
    to_user = root.findtext("ToUserName") or ""

    if msg_type == "event" and (root.findtext("Event") or "").lower() == "subscribe":
        return _reply(
            _build_reply_xml(
                openid, to_user,
                "🎉 欢迎关注\n\n请把网页上显示的 6 位验证码发送给我，即可完成登录。\n验证码 5 分钟内有效。",
            ),
            params,
        )

    if msg_type == "text":
        content = (root.findtext("Content") or "").strip()
        if content.isdigit() and len(content) == 6:
            s = sessions.consume(content, openid)
            if s:
                logger.info(f"登录成功: code={content} openid={openid}")
                login_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                return _reply(
                    _build_reply_xml(
                        openid, to_user,
                        f"✅ 登录成功\n\n时间：{login_time}\nopenid：{openid}\n\n网页将自动跳转，请勿关闭。",
                    ),
                    params,
                )
            return _reply(
                _build_reply_xml(openid, to_user, "❌ 验证码错误或已过期\n\n请回到网页查看最新的 6 位验证码再发送。"),
                params,
            )
        return _reply(
            _build_reply_xml(openid, to_user, "请发送网页上显示的 6 位数字验证码完成登录。"),
            params,
        )

    return PlainTextResponse("success")


@app.get("/", response_class=HTMLResponse)
def index():
    return Path(ROOT / "static" / "index.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host=settings.host, port=settings.port)
