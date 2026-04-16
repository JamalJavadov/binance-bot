import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import AsyncSessionLocal, create_schema
from app.routers.credentials import router as credentials_router
from app.routers.history import router as history_router
from app.routers.auto_mode import router as auto_mode_router
from app.routers.orders import router as orders_router
from app.routers.scan import router as scan_router
from app.routers.settings import router as settings_router
from app.routers.signals import router as signals_router
from app.routers.status import account_router, router as status_router
from app.services.auto_mode import AutoModeService
from app.services.binance_gateway import BinanceGateway
from app.services.lifecycle_monitor import LifecycleMonitor
from app.services.market_health import MarketHealthService
from app.services.notifier import Notifier
from app.services.order_manager import OrderManager
from app.services.position_observer import PositionObserver
from app.services.scanner import ScannerService
from app.services.scheduler import SchedulerService
from app.services.settings import seed_settings
from app.services.user_data_stream import UserDataStreamSupervisor
from app.services.ws_manager import WebSocketManager


settings = get_settings()
configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.auto_create_schema:
        await create_schema()
    async with AsyncSessionLocal() as session:
        await seed_settings(session)

    gateway = BinanceGateway()
    await gateway.sync_server_time()
    ws_manager = WebSocketManager()
    notifier = Notifier()
    market_health = MarketHealthService(gateway)
    order_manager = OrderManager(gateway, ws_manager, notifier)
    position_observer = PositionObserver(gateway, order_manager)
    scanner_service = ScannerService(gateway, ws_manager, order_manager, notifier, market_health=market_health)
    auto_mode_service = AutoModeService(
        scanner_service,
        order_manager,
        gateway,
        ws_manager,
        AsyncSessionLocal,
        market_health=market_health,
    )
    lifecycle_monitor = LifecycleMonitor(
        order_manager,
        position_observer,
        auto_mode_service=auto_mode_service,
        poll_seconds=settings.lifecycle_poll_seconds,
    )
    user_stream_supervisor = UserDataStreamSupervisor(
        gateway,
        AsyncSessionLocal,
        order_manager,
        lifecycle_monitor,
    )
    scheduler_service = SchedulerService(auto_mode_service, AsyncSessionLocal)

    app.state.gateway = gateway
    app.state.ws_manager = ws_manager
    app.state.notifier = notifier
    app.state.market_health = market_health
    app.state.order_manager = order_manager
    app.state.position_observer = position_observer
    app.state.scanner_service = scanner_service
    app.state.auto_mode_service = auto_mode_service
    app.state.lifecycle_monitor = lifecycle_monitor
    app.state.user_stream_supervisor = user_stream_supervisor
    app.state.scheduler_service = scheduler_service
    app.state.session_factory = AsyncSessionLocal
    app.state.scan_task = None

    await market_health.start()
    await lifecycle_monitor.start()
    await user_stream_supervisor.start()
    await scheduler_service.start()

    try:
        yield
    finally:
        if app.state.scan_task is not None and not app.state.scan_task.done():
            app.state.scan_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await app.state.scan_task
        await scheduler_service.stop()
        await auto_mode_service.stop()
        await user_stream_supervisor.stop()
        await lifecycle_monitor.stop()
        await market_health.stop()
        await gateway.close()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

app.include_router(credentials_router, prefix=settings.api_prefix)
app.include_router(history_router, prefix=settings.api_prefix)
app.include_router(auto_mode_router, prefix=settings.api_prefix)
app.include_router(settings_router, prefix=settings.api_prefix)
app.include_router(status_router, prefix=settings.api_prefix)
app.include_router(account_router, prefix=settings.api_prefix)
app.include_router(scan_router, prefix=settings.api_prefix)
app.include_router(signals_router, prefix=settings.api_prefix)
app.include_router(orders_router, prefix=settings.api_prefix)


@app.get("/")
async def root() -> dict[str, str]:
    return {"message": settings.app_name}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await app.state.ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        app.state.ws_manager.disconnect(websocket)
