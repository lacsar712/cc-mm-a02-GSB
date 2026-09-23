from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, Float, ForeignKey, String, create_engine
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
    opened_by: Mapped[str] = mapped_column(String(64))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="开启中")
    closed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ShiftEvent(Base):
    __tablename__ = "shift_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[int | None] = mapped_column(ForeignKey("shifts.id"), nullable=True)
    shift_name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(20))
    actor: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detail: Mapped[str] = mapped_column(String(200), default="")


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class ShiftOpenIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    start_at: datetime
    end_at: datetime

    def normalized(self) -> "ShiftOpenIn":
        if self.start_at.tzinfo is None:
            self.start_at = self.start_at.replace(tzinfo=timezone.utc)
        if self.end_at.tzinfo is None:
            self.end_at = self.end_at.replace(tzinfo=timezone.utc)
        return self


class ShiftWindowIn(BaseModel):
    start_at: datetime
    end_at: datetime

    def normalized(self) -> "ShiftWindowIn":
        if self.start_at.tzinfo is None:
            self.start_at = self.start_at.replace(tzinfo=timezone.utc)
        if self.end_at.tzinfo is None:
            self.end_at = self.end_at.replace(tzinfo=timezone.utc)
        return self


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


def shift_dict(s: Shift) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "start_at": s.start_at.isoformat(),
        "end_at": s.end_at.isoformat(),
        "opened_by": s.opened_by,
        "opened_at": s.opened_at.isoformat(),
        "status": s.status,
        "closed_by": s.closed_by,
        "closed_at": s.closed_at.isoformat() if s.closed_at else None,
    }


def event_dict(e: ShiftEvent) -> dict:
    return {
        "id": e.id,
        "shift_name": e.shift_name,
        "kind": e.kind,
        "actor": e.actor,
        "occurred_at": e.occurred_at.isoformat(),
        "detail": e.detail,
    }


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
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        shift = db.query(Shift).filter(Shift.status == "开启中").one_or_none()
        if shift is None:
            raise HTTPException(status_code=409, detail="当前没有开启中的班次，上报被拒绝（已关闭）")
        if not (shift.start_at <= now <= shift.end_at):
            raise HTTPException(
                status_code=409,
                detail=f"当前时刻不在班次「{shift.name}」起止窗内，上报被拒绝（窗外）",
            )
        level, note = classify(body.ch4_pct)
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
        return [shift_dict(s) for s in rows]
    finally:
        db.close()


@app.get("/api/shift-events")
def list_shift_events(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(ShiftEvent).order_by(ShiftEvent.id.desc()).limit(100).all()
        return [event_dict(e) for e in rows]
    finally:
        db.close()


@app.post("/api/shifts", status_code=201)
def open_shift(body: ShiftOpenIn, user: dict = Depends(require_writer)):
    body.normalized()
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="班次名不能为空")
    if body.end_at <= body.start_at:
        raise HTTPException(status_code=422, detail="止刻必须晚于起刻")
    db = SessionLocal()
    try:
        active = db.query(Shift).filter(Shift.status == "开启中").one_or_none()
        if active is not None:
            raise HTTPException(
                status_code=409,
                detail=f"已有开启中的班次「{active.name}」，请先关闭再开新班",
            )
        now = datetime.now(timezone.utc)
        shift = Shift(
            name=name,
            start_at=body.start_at,
            end_at=body.end_at,
            opened_by=user["username"],
            opened_at=now,
            status="开启中",
        )
        db.add(shift)
        db.flush()
        db.add(
            ShiftEvent(
                shift_id=shift.id,
                shift_name=shift.name,
                kind="开启",
                actor=user["username"],
                occurred_at=now,
                detail=f"起 {body.start_at.isoformat()} / 止 {body.end_at.isoformat()}",
            )
        )
        db.commit()
        db.refresh(shift)
        return shift_dict(shift)
    finally:
        db.close()


@app.patch("/api/shifts/{shift_id}/window")
def update_shift_window(shift_id: int, body: ShiftWindowIn, user: dict = Depends(require_writer)):
    body.normalized()
    if body.end_at <= body.start_at:
        raise HTTPException(status_code=422, detail="止刻必须晚于起刻")
    db = SessionLocal()
    try:
        shift = db.get(Shift, shift_id)
        if shift is None:
            raise HTTPException(status_code=404, detail="班次不存在")
        if shift.status != "开启中":
            raise HTTPException(status_code=409, detail="班次已关闭，不能再改起止时刻")
        old = f"起 {shift.start_at.isoformat()} / 止 {shift.end_at.isoformat()}"
        shift.start_at = body.start_at
        shift.end_at = body.end_at
        db.add(
            ShiftEvent(
                shift_id=shift.id,
                shift_name=shift.name,
                kind="改窗",
                actor=user["username"],
                occurred_at=datetime.now(timezone.utc),
                detail=f"{old} → 起 {body.start_at.isoformat()} / 止 {body.end_at.isoformat()}",
            )
        )
        db.commit()
        db.refresh(shift)
        return shift_dict(shift)
    finally:
        db.close()


@app.post("/api/shifts/{shift_id}/close")
def close_shift(shift_id: int, user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        shift = db.get(Shift, shift_id)
        if shift is None:
            raise HTTPException(status_code=404, detail="班次不存在")
        if shift.status != "开启中":
            raise HTTPException(status_code=409, detail="班次已关闭，无需重复关闭")
        now = datetime.now(timezone.utc)
        shift.status = "已关闭"
        shift.closed_by = user["username"]
        shift.closed_at = now
        db.add(
            ShiftEvent(
                shift_id=shift.id,
                shift_name=shift.name,
                kind="关闭",
                actor=user["username"],
                occurred_at=now,
                detail="",
            )
        )
        db.commit()
        db.refresh(shift)
        return shift_dict(shift)
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
