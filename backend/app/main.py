from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import AwareDatetime, BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, Float, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import classify


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://app:app@localhost:54391/methane"
    jwt_secret: str = "mine-methane-dev-secret"


settings = Settings()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "gasman": {"role": "writer", "password_hash": pwd.hash("gas123456")},
    "viewer": {"role": "reader", "password_hash": pwd.hash("view123456")},
}

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


class Reading(Base):
    __tablename__ = "readings"
    id: Mapped[int] = mapped_column(primary_key=True)
    site: Mapped[str] = mapped_column(String(80))
    ch4_pct: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Shift(Base):
    __tablename__ = "shifts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20))  # 开启中 / 已关闭
    opened_by: Mapped[str] = mapped_column(String(64))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ShiftEvent(Base):
    """班次大事记：目前记录关闭事件（班次名、关闭人、关闭时刻）。"""

    __tablename__ = "shift_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    shift_name: Mapped[str] = mapped_column(String(80))
    closed_by: Mapped[str] = mapped_column(String(64))
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class ShiftOpenIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    start_at: AwareDatetime
    end_at: AwareDatetime


class ShiftWindowIn(BaseModel):
    start_at: AwareDatetime
    end_at: AwareDatetime


def as_aware(dt: datetime) -> datetime:
    # Postgres 的 timestamptz 读出来自带时区；SQLite 等库读出的是朴素时间，统一按 UTC 补齐。
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def shift_dict(s: Shift) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "start_at": as_aware(s.start_at).isoformat(),
        "end_at": as_aware(s.end_at).isoformat(),
        "status": s.status,
        "opened_by": s.opened_by,
        "opened_at": as_aware(s.opened_at).isoformat(),
        "closed_by": s.closed_by,
        "closed_at": as_aware(s.closed_at).isoformat() if s.closed_at else None,
    }


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="无效令牌") from exc
    username = payload.get("sub")
    if username not in USERS:
        raise HTTPException(status_code=401, detail="无效令牌")
    return {"username": username, "role": payload.get("role")}


def require_writer(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "writer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅瓦斯检查员可操作")
    return user


sockets: set[WebSocket] = set()
app = FastAPI(title="矿井瓦斯班测台")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(Reading).count() == 0:
            now = datetime.now(timezone.utc)
            for site, ch4 in (("东翼-12", 0.35), ("回风巷", 1.4)):
                level, note = classify(ch4)
                db.add(
                    Reading(
                        site=site,
                        ch4_pct=ch4,
                        level=level,
                        note=note,
                        created_by="gasman",
                        created_at=now,
                    )
                )
            db.commit()
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "mine-methane-shift"}


@app.post("/api/auth/login")
def login(body: LoginIn):
    user = USERS.get(body.username.strip())
    if not user or not pwd.verify(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode(
        {"sub": body.username.strip(), "role": user["role"], "exp": exp},
        settings.jwt_secret,
        algorithm="HS256",
    )
    return {"access_token": token, "username": body.username.strip(), "role": user["role"]}


@app.get("/api/readings")
def list_readings(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(Reading).order_by(Reading.id.desc()).all()
        return [
            {
                "id": r.id,
                "site": r.site,
                "ch4_pct": r.ch4_pct,
                "level": r.level,
                "note": r.note,
                "created_by": r.created_by,
            }
            for r in rows
        ]
    finally:
        db.close()


@app.post("/api/readings", status_code=201)
async def create_reading(body: ReadingIn, user: dict = Depends(require_writer)):
    level, note = classify(body.ch4_pct)
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        shift = db.query(Shift).filter(Shift.status == "开启中").first()
        if shift is None:
            raise HTTPException(status_code=403, detail="当前班次已关闭，禁止上报")
        if not (as_aware(shift.start_at) <= now <= as_aware(shift.end_at)):
            raise HTTPException(status_code=403, detail="当前时刻在班次窗外，禁止上报")
        row = Reading(
            site=body.site.strip(),
            ch4_pct=body.ch4_pct,
            level=level,
            note=note,
            created_by=user["username"],
            created_at=now,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        payload = {"id": row.id, "site": row.site, "ch4_pct": row.ch4_pct, "level": row.level, "note": row.note}
    finally:
        db.close()
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)
    return payload


@app.get("/api/shifts")
def list_shifts(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(Shift).order_by(Shift.id.desc()).all()
        return [shift_dict(r) for r in rows]
    finally:
        db.close()


@app.get("/api/shifts/events")
def list_shift_events(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(ShiftEvent).order_by(ShiftEvent.id.desc()).all()
        return [
            {
                "id": e.id,
                "shift_name": e.shift_name,
                "closed_by": e.closed_by,
                "closed_at": as_aware(e.closed_at).isoformat(),
            }
            for e in rows
        ]
    finally:
        db.close()


@app.post("/api/shifts", status_code=201)
def open_shift(body: ShiftOpenIn, user: dict = Depends(require_writer)):
    if body.end_at <= body.start_at:
        raise HTTPException(status_code=422, detail="结束时刻必须晚于开始时刻")
    db = SessionLocal()
    try:
        if db.query(Shift).filter(Shift.status == "开启中").first() is not None:
            raise HTTPException(status_code=409, detail="已有开启中的班次，再开新班必须先关闭旧班")
        row = Shift(
            name=body.name.strip(),
            start_at=body.start_at,
            end_at=body.end_at,
            status="开启中",
            opened_by=user["username"],
            opened_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return shift_dict(row)
    finally:
        db.close()


@app.put("/api/shifts/{shift_id}")
def update_shift_window(shift_id: int, body: ShiftWindowIn, user: dict = Depends(require_writer)):
    if body.end_at <= body.start_at:
        raise HTTPException(status_code=422, detail="结束时刻必须晚于开始时刻")
    db = SessionLocal()
    try:
        row = db.get(Shift, shift_id)
        if row is None:
            raise HTTPException(status_code=404, detail="班次不存在")
        if row.status != "开启中":
            raise HTTPException(status_code=409, detail="班次已关闭，不能修改起止")
        row.start_at = body.start_at
        row.end_at = body.end_at
        db.commit()
        db.refresh(row)
        return shift_dict(row)
    finally:
        db.close()


@app.post("/api/shifts/{shift_id}/close")
def close_shift(shift_id: int, user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        row = db.get(Shift, shift_id)
        if row is None:
            raise HTTPException(status_code=404, detail="班次不存在")
        if row.status != "开启中":
            raise HTTPException(status_code=409, detail="班次已关闭")
        now = datetime.now(timezone.utc)
        row.status = "已关闭"
        row.closed_by = user["username"]
        row.closed_at = now
        db.add(ShiftEvent(shift_name=row.name, closed_by=user["username"], closed_at=now))
        db.commit()
        db.refresh(row)
        return shift_dict(row)
    finally:
        db.close()


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)
