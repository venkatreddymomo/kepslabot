"""Application lifespan handler + periodic log-cleanup task.

Relocated verbatim from main.py (behavior-preserving). Owns DB init, DB log
sink start/stop, KB restore, per-hotel cache prewarm, prompt-registry seeding,
and service-prompt regeneration on startup.
"""
import asyncio
from contextlib import asynccontextmanager
from time import perf_counter

from fastapi import FastAPI

from config.settings import settings
from models.database import init_db
from services import new_detailed_logger
from services.db_config_service import db_config_service
from services.db_log_sink import db_log_sink
from services.log_retention_service import log_retention_service
from services.log_setup_service import log_setup_service
from services.step_trace_service import step_trace_service

from bootstrap.console_logging import configure_console_logging


async def _periodic_ird_book_sweep_task() -> None:
    """Drop in-room dining order books nobody has touched for a day.

    The book holds ten order slots per stay-session; sweeping the row is what
    lets those slots recycle, which is the whole reason ten is enough. Nothing is
    lost: every placed order is already on record as an external ticket, and
    recall falls back to those when the book has no row.
    """
    hours = max(1, int(getattr(settings, "ird_order_book_sweep_hours", 24) or 24))
    while True:
        # Hourly, so a row is never more than an hour past its welcome, and the
        # first sweep does not race the rest of startup.
        await asyncio.sleep(3600.0)
        try:
            from services import ird_cart_store

            swept = await ird_cart_store.sweep(older_than_hours=hours)
            if swept:
                step_trace_service.log_event(
                    "ird_order_book_sweep",
                    payload={"rows_deleted": swept, "older_than_hours": hours},
                    component="main._periodic_ird_book_sweep_task",
                    step="periodic_sweep",
                    stage="completed",
                )
        except asyncio.CancelledError:
            # Must propagate, or shutdown waits on a task that swallowed its own
            # cancellation.
            raise
        except Exception as exc:
            # A sweep that cannot run leaves stale rows, never a broken chat.
            step_trace_service.log_event(
                "ird_order_book_sweep",
                payload={"error": repr(exc)},
                component="main._periodic_ird_book_sweep_task",
                step="periodic_sweep",
                stage="failed",
            )


async def _periodic_log_cleanup_task() -> None:
    """Run age-based log cleanup in the background."""
    interval_minutes = max(
        5,
        int(getattr(settings, "log_retention_cleanup_interval_minutes", 360) or 360),
    )
    interval_seconds = float(interval_minutes * 60)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            summary = log_retention_service.cleanup_old_logs()
            db_pruned = {}
            try:
                if bool(getattr(settings, "db_logging_enabled", True)):
                    # Roll up BEFORE pruning: prune() deletes raw rows older
                    # than the retention window, and the rollup is the only
                    # durable record of what they cost. Prune-first loses the
                    # spend of any row that aged past the window since the last
                    # rollup — e.g. a short session whose rows are already >7d
                    # old by the next boot.
                    await db_log_sink.rollup_llm_daily()
                    db_pruned = await db_log_sink.prune()
            except Exception:
                db_pruned = {}
            step_trace_service.log_event(
                "log_retention_cleanup",
                payload={"files": summary, "db_rows_deleted": db_pruned},
                component="main._periodic_log_cleanup_task",
                step="periodic_cleanup",
                stage="completed",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            step_trace_service.log_event(
                "log_retention_cleanup_failed",
                payload={},
                component="main._periodic_log_cleanup_task",
                step="periodic_cleanup",
                stage="failed",
                error=str(exc),
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    cleanup_task: asyncio.Task | None = None
    startup_regen_task: asyncio.Task | None = None

    # Startup
    # Re-assert console logging AFTER uvicorn has configured its own loggers, so
    # the app's request lines ([API] ...) and error tracebacks print to the
    # terminal (idempotent — won't stack handlers).
    configure_console_logging()
    from services.flow_logger import ensure_log_files
    ensure_log_files()
    log_setup_summary = log_setup_service.ensure_configured_log_files()
    step_trace_service.log_event(
        "log_files_ensured",
        payload=log_setup_summary,
        component="main.lifespan",
        step="log_bootstrap",
        stage="completed",
    )

    if bool(getattr(settings, "log_retention_cleanup_on_startup", True)):
        cleanup_summary = log_retention_service.cleanup_old_logs()
        step_trace_service.log_event(
            "log_retention_cleanup",
            payload=cleanup_summary,
            component="main.lifespan",
            step="startup_cleanup",
            stage="completed",
        )

    print(f"Starting {settings.app_name}...")
    print(f"Environment: {settings.app_env}")

    try:
        from sqlalchemy.engine import make_url
        _db_url = make_url(str(settings.database_url))
        _backend = str(_db_url.get_backend_name() or "?").lower()
        _driver = str(_db_url.get_driver_name() or "?").lower()
        _host = _db_url.host or "?"
        _db = _db_url.database or "?"
        if _backend == "mysql":
            print(f"DB backend: MySQL ({_driver}) host={_host} db={_db}  <-- WARNING: not Postgres")
        elif _backend in {"postgresql", "postgres"}:
            print(f"DB backend: PostgreSQL ({_driver}) host={_host} db={_db}")
        else:
            print(f"DB backend: {_backend} ({_driver}) url={_db_url.render_as_string(hide_password=True)}")
    except Exception as _e:
        print(f"DB backend: <unable to introspect: {_e}>")

    # ── new_detailed_logger: startup begin ──────────────────────────────────
    new_detailed_logger.log_startup_begin(
        app_name=str(settings.app_name),
        host=str(settings.host),
        port=int(settings.port),
        env=str(settings.app_env),
    )

    # Initialize database
    db_init_timeout = max(
        5.0,
        float(getattr(settings, "admin_db_fast_fallback_timeout_seconds", 30.0) or 30.0),
    )
    try:
        await asyncio.wait_for(init_db(), timeout=db_init_timeout)
        print("Database initialized")
        new_detailed_logger.log_db_init(success=True)
    except asyncio.TimeoutError:
        timeout_msg = f"Database init timed out after {db_init_timeout:.0f}s"
        print(f"{timeout_msg} (continuing with startup)")
        new_detailed_logger.log_db_init(success=False, error=timeout_msg)
    except Exception as e:
        print(f"Database init failed (will use in-memory): {e}")
        new_detailed_logger.log_db_init(success=False, error=str(e))

    # Start the DB log sink (background batched writer) once the loop + DB are up.
    try:
        db_log_sink.start()
        step_trace_service.log_event(
            "db_log_sink_started",
            payload={"source_env": db_log_sink.source_env, "enabled": db_log_sink.enabled},
            component="main.lifespan",
            step="db_log_sink",
            stage="started",
        )
        if bool(getattr(settings, "db_logging_enabled", True)):
            # Rollup first, then prune — see _periodic_log_cleanup_task.
            await db_log_sink.rollup_llm_daily()
            await db_log_sink.prune()
    except Exception as _sink_exc:
        print(f"db_log_sink start skipped: {_sink_exc}")

    startup_step_timeout = max(
        5.0,
        float(getattr(settings, "startup_noncritical_step_timeout_seconds", 45.0) or 45.0),
    )
    prompt_regen_step_timeout = max(
        startup_step_timeout,
        float(getattr(settings, "startup_prompt_regen_timeout_seconds", 180.0) or 180.0),
    )

    # Restore KB files from DB if missing from disk, then sync services to JSON.
    restored = 0
    try:
        from services.rag_service import rag_service as _rag

        print(
            f"Startup step: restore_kb_files (timeout={startup_step_timeout:.0f}s)",
            flush=True,
        )
        restore_started = perf_counter()
        try:
            restored = int(
                await asyncio.wait_for(
                    db_config_service.restore_kb_files(str(_rag.kb_dir)),
                    timeout=startup_step_timeout,
                )
                or 0
            )
            print(
                "Startup step done: restore_kb_files "
                f"({(perf_counter() - restore_started) * 1000:.1f} ms, restored={restored})",
                flush=True,
            )
        except asyncio.TimeoutError:
            print(
                "Startup step timeout: restore_kb_files "
                f"exceeded {startup_step_timeout:.0f}s (continuing)",
                flush=True,
            )
            restored = 0

        if restored:
            print(f"Restored {restored} KB file(s) from database")

        print(
            f"Startup step: get_services sync (timeout={startup_step_timeout:.0f}s)",
            flush=True,
        )
        services_sync_started = perf_counter()
        try:
            await asyncio.wait_for(
                db_config_service.get_services(),
                timeout=startup_step_timeout,
            )
            print(
                "Startup step done: get_services sync "
                f"({(perf_counter() - services_sync_started) * 1000:.1f} ms)",
                flush=True,
            )
        except asyncio.TimeoutError:
            print(
                "Startup step timeout: get_services sync "
                f"exceeded {startup_step_timeout:.0f}s (continuing)",
                flush=True,
            )
        except Exception as e:
            if e.__class__.__name__ == "HotelNotFoundError":
                print(
                    "Startup step skipped: get_services sync "
                    f"({e})",
                    flush=True,
                )
            else:
                raise

        # Per-hotel cache prewarm: hit the DB once per active hotel during
        # startup so the in-process services cache + hotel_id_by_code map
        # is populated for every tenant. Without this, the first chat turn
        # of every fresh hotel session pays the cold VPN-to-MySQL roundtrip
        # (~5-10s in iconiqa logs) which dominates user-perceived latency.
        # Runs in parallel with bounded concurrency. Failures per hotel are
        # logged but never block the rest of startup.
        try:
            from sqlalchemy import select as _select_h
            from models.database import AsyncSessionLocal as _AsyncSL, Hotel as _Hotel
            prewarm_started = perf_counter()
            async with _AsyncSL() as _hsession:
                # No is_active filter — older rows / case-insensitive flags
                # were excluding active hotels and skipping the prewarm.
                _hotel_rows = (await _hsession.execute(
                    _select_h(_Hotel)
                )).scalars().all()
                _hotel_codes = [str(h.code or "").strip() for h in _hotel_rows if h.code]

            async def _prewarm_one(_code: str) -> None:
                try:
                    with db_config_service.scoped_hotel(_code):
                        # 1. Services cache (in-process, TTL 1hr)
                        await db_config_service.get_services()
                        # 2. Book catalog cache (used by main_router on every turn)
                        try:
                            from services.kb_table_reader import get_structured_book_catalog_db
                            await get_structured_book_catalog_db(
                                hotel_code=_code,
                                property_slugs=None,
                                only_active=True,
                            )
                        except Exception as _bk_e:
                            print(f"  prewarm book_catalog failed for {_code}: {_bk_e}", flush=True)
                        # 3. KB documents cache (used by book_answerer KB context)
                        try:
                            from services.config_service import config_service as _cs
                            _cs._load_kb_documents_from_db_sync(_code.upper())
                        except Exception as _kb_e:
                            print(f"  prewarm kb_docs failed for {_code}: {_kb_e}", flush=True)
                        # 4. Image tool caches (is_enabled + candidate list).
                        # Without these the smart_image attach step paid 2-3
                        # VPN roundtrips per turn (~6s) for data that rarely
                        # changes between admin uploads.
                        try:
                            from services.image_tool_service import image_tool_service as _its
                            await _its.is_enabled_for_hotel(_code)
                            await _its.list_candidate_assets(hotel_code=_code, limit=0)
                        except Exception as _im_e:
                            print(f"  prewarm images failed for {_code}: {_im_e}", flush=True)
                        # 5. Capabilities + business_info + intents + knowledge_config —
                        # these are the 6 calls that _overlay_capabilities_from_db_config
                        # parallelizes, but we can prewarm them so even the first
                        # gather is mostly cache hits.
                        try:
                            await db_config_service.get_business_info()
                        except Exception:
                            pass
                        try:
                            await db_config_service.get_capabilities()
                        except Exception:
                            pass
                        try:
                            await db_config_service.get_intents()
                        except Exception:
                            pass
                        try:
                            await db_config_service.get_knowledge_config()
                        except Exception:
                            pass
                except Exception as _e:
                    print(f"  prewarm get_services failed for {_code}: {_e}", flush=True)

            if _hotel_codes:
                # Parallelism cap — too many concurrent VPN sessions choke
                # the connection pool, too few wastes time. 4 is a balance.
                _sem = asyncio.Semaphore(4)
                async def _bounded_prewarm(_code: str) -> None:
                    async with _sem:
                        await _prewarm_one(_code)
                await asyncio.wait_for(
                    asyncio.gather(*[_bounded_prewarm(c) for c in _hotel_codes]),
                    timeout=startup_step_timeout,
                )
                _prewarm_ms = round((perf_counter() - prewarm_started) * 1000.0, 1)
                print(
                    f"Startup step done: prewarm services for {len(_hotel_codes)} hotel(s) "
                    f"({_prewarm_ms:.1f} ms)",
                    flush=True,
                )
                step_trace_service.log_event(
                    "lifespan_prewarm_done",
                    payload={
                        "hotel_codes": _hotel_codes,
                        "duration_ms": _prewarm_ms,
                    },
                    component="main.lifespan",
                    step="hotel_prewarm",
                    stage="completed",
                )
            else:
                step_trace_service.log_event(
                    "lifespan_prewarm_no_hotels",
                    payload={"hotel_count": 0},
                    component="main.lifespan",
                    step="hotel_prewarm",
                    stage="skipped",
                )
        except asyncio.TimeoutError:
            print(
                f"Startup step timeout: hotel prewarm exceeded {startup_step_timeout:.0f}s (continuing)",
                flush=True,
            )
            step_trace_service.log_event(
                "lifespan_prewarm_timeout",
                payload={"timeout_s": startup_step_timeout},
                component="main.lifespan",
                step="hotel_prewarm",
                stage="failed",
                error="timeout",
            )
        except Exception as _e:
            print(f"Startup step failed (non-fatal): hotel prewarm: {_e}", flush=True)
            step_trace_service.log_event(
                "lifespan_prewarm_failed",
                payload={"error": str(_e)},
                component="main.lifespan",
                step="hotel_prewarm",
                stage="failed",
                error=str(_e),
            )

        new_detailed_logger.log_kb_restore(restored=restored)
    except Exception as e:
        print(f"Startup DB sync failed (non-fatal): {e}")
        new_detailed_logger.log_kb_restore(restored=restored, error=str(e))

    # Sync prompt markdown files (prompts/defaults/<industry>/*.md) into the
    # DB-backed registry. Safe to run on every startup — hotel overrides are
    # never touched; industry defaults only change when the file hash differs.
    updated_keys = []
    try:
        from services.prompt_seed_service import seed_prompts_from_files

        print(
            f"Startup step: prompt registry seed (timeout={startup_step_timeout:.0f}s)",
            flush=True,
        )
        seed_started = perf_counter()
        try:
            inserted, updated, unchanged, updated_keys = await asyncio.wait_for(
                seed_prompts_from_files(),
                timeout=startup_step_timeout,
            )
            print(
                "Prompt registry seed: "
                f"inserted={inserted} updated={updated} unchanged={unchanged} "
                f"({(perf_counter() - seed_started) * 1000:.1f} ms)",
                flush=True,
            )
        except asyncio.TimeoutError:
            print(
                "Prompt registry seed timed out "
                f"after {startup_step_timeout:.0f}s (continuing)",
                flush=True,
            )
            updated_keys = []
    except Exception as e:
        print(f"Prompt registry seed failed (non-fatal): {e}")
        updated_keys = []

    # If a service-writer template (form/text mode rules, instructions) changed,
    # the per-service generated_system_prompt rows in DB are now stale. Regenerate
    # them so the new template content reaches the runtime LLM without a manual
    # admin re-save.
    _service_writer_keys = {
        "service_writer.form_mode_rules",
        "service_writer.text_mode_rules",
        "service_writer.instructions",
        "service_writer.bot_briefing",
        "service_writer.multi_property_rule",
    }
    if updated_keys and any(k in _service_writer_keys for k in updated_keys):
        changed_writer_keys = [k for k in updated_keys if k in _service_writer_keys]

        async def _regen_service_prompts() -> None:
            try:
                from services.prompt_writer_service import generate_service_system_prompt

                print(
                    "Service-writer templates changed "
                    f"({', '.join(changed_writer_keys)}); "
                    "regenerating per-service generated_system_prompt rows in background...",
                    flush=True,
                )

                regen_step_started = perf_counter()
                regen_count = 0
                from sqlalchemy import select as _select_h
                from models.database import AsyncSessionLocal as _AsyncSL, Hotel as _Hotel

                async with _AsyncSL() as _hsession:
                    _hotel_rows = (await _hsession.execute(_select_h(_Hotel))).scalars().all()
                    _hotel_codes = [str(h.code or "").strip() for h in _hotel_rows if h.code]

                for hotel_code in _hotel_codes:
                    try:
                        with db_config_service.scoped_hotel(hotel_code):
                            services = await asyncio.wait_for(
                                db_config_service.get_services(),
                                timeout=startup_step_timeout,
                            )
                            services = services or []
                            for svc in services:
                                if not isinstance(svc, dict):
                                    continue
                                if bool(svc.get("generated_system_prompt_override")):
                                    # Admin manually edited - never overwrite their edits.
                                    continue
                                svc_id = str(svc.get("id") or "").strip()
                                if not svc_id:
                                    continue
                                try:
                                    prompt = await generate_service_system_prompt(svc)
                                    if prompt:
                                        await db_config_service.save_generated_prompt(svc_id, prompt)
                                        regen_count += 1
                                except Exception as _exc:
                                    print(f"  service_prompt_regen failed for {hotel_code}/{svc_id}: {_exc}")
                    except Exception as _hotel_exc:
                        if _hotel_exc.__class__.__name__ == "HotelNotFoundError":
                            print(f"  service_prompt_regen skipped for {hotel_code}: {_hotel_exc}")
                        else:
                            raise

                print(
                    "Service prompts regenerated: "
                    f"{regen_count} ({(perf_counter() - regen_step_started) * 1000:.1f} ms)",
                    flush=True,
                )
            except asyncio.TimeoutError:
                print(
                    "Service prompt regeneration startup step timed out "
                    f"after {startup_step_timeout:.0f}s (background task ended)",
                    flush=True,
                )
            except Exception as e:
                print(f"Service prompt regeneration failed (non-fatal): {e}")

        startup_regen_task = asyncio.create_task(
            asyncio.wait_for(_regen_service_prompts(), timeout=prompt_regen_step_timeout)
        )

    display_host = str(settings.host)
    if display_host in {"0.0.0.0", "::"}:
        display_host = "localhost"
    print(f"Server: http://{display_host}:{settings.port}")
    print(f"API Docs: http://{display_host}:{settings.port}/docs")
    print(f"Test Chat: http://{display_host}:{settings.port}/chat")
    print(f"Admin Portal: http://{display_host}:{settings.port}/admin")
    print(f"KB Scraper (same port): http://{display_host}:{settings.port}/kb-scraper/")

    # ── new_detailed_logger: server ready ───────────────────────────────────
    new_detailed_logger.log_startup_ready(
        host=str(settings.host),
        port=int(settings.port),
    )

    if bool(getattr(settings, "log_retention_enabled", True)):
        cleanup_task = asyncio.create_task(_periodic_log_cleanup_task())

    ird_book_sweep_task = None
    if bool(getattr(settings, "ird_order_book_enabled", False)):
        ird_book_sweep_task = asyncio.create_task(_periodic_ird_book_sweep_task())

    # Admin-managed pre-booking settings: load every hotel's once, so the
    # synchronous readers in the chat have them from the first turn.
    try:
        from services import prebooking_settings_service

        _managed = await asyncio.wait_for(prebooking_settings_service.warm_all(), timeout=60)
        print(f"Pre-booking settings loaded ({_managed} admin-managed hotel(s))")
    except Exception as _settings_exc:
        print(f"Pre-booking settings warm-up skipped: {_settings_exc!r}")

    yield

    # Shutdown
    if ird_book_sweep_task is not None:
        ird_book_sweep_task.cancel()
    if cleanup_task is not None:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    if startup_regen_task is not None:
        startup_regen_task.cancel()
        try:
            await startup_regen_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    # Flush + stop the DB log sink so queued rows aren't lost on shutdown.
    try:
        await db_log_sink.stop()
    except Exception:
        pass

    print("Shutting down...")
    new_detailed_logger.log_shutdown()
