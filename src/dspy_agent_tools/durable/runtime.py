"""Process-wide DBOS runtime configuration and lifecycle."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from ._dbos import load_dbos
from .exceptions import DurableError
from .serializer import CloudpickleSerializer

DEFAULT_APP_NAME = "dspy-agent-tools"


@dataclass
class RuntimeConfig:
    name: str = DEFAULT_APP_NAME
    database_url: str | None = None
    manage_runtime: bool = True
    application_version: str | None = None
    executor_id: str | None = None
    log_level: str | None = None
    verbose: bool = False
    authorizer: Callable[[str, str, str | None, dict[str, Any] | None], bool] | None = (
        None
    )


@dataclass
class _Runtime:
    config: RuntimeConfig = field(default_factory=RuntimeConfig)
    launched: bool = False
    client_instance: Any = None
    queues: dict[str, dict[str, Any]] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)
    serializer: Any = field(default_factory=CloudpickleSerializer)

    def configure(
        self,
        *,
        name: str | None = None,
        database_url: str | None = None,
        manage_runtime: bool | None = None,
        application_version: str | None = None,
        executor_id: str | None = None,
        log_level: str | None = None,
        verbose: bool | None = None,
        authorizer: (
            Callable[[str, str, str | None, dict[str, Any] | None], bool] | None
        ) = None,
    ) -> None:
        """Set the runtime configuration before the first execution."""
        incoming = RuntimeConfig(
            name=name or self.config.name,
            database_url=(
                database_url if database_url is not None else self.config.database_url
            ),
            manage_runtime=(
                manage_runtime
                if manage_runtime is not None
                else self.config.manage_runtime
            ),
            application_version=(
                application_version
                if application_version is not None
                else self.config.application_version
            ),
            executor_id=(
                executor_id if executor_id is not None else self.config.executor_id
            ),
            log_level=log_level or self.config.log_level,
            verbose=verbose if verbose is not None else self.config.verbose,
            authorizer=(
                authorizer if authorizer is not None else self.config.authorizer
            ),
        )
        with self.lock:
            if (
                self.launched
                and replace(incoming, verbose=self.config.verbose) != self.config
            ):
                raise DurableError(
                    "Durable.configure() cannot change configuration after the "
                    "runtime has launched. Configure once at startup."
                )
            self.config = incoming
            self._apply_verbosity(incoming.verbose)

    def set_verbose(self, verbose: bool) -> None:
        """Toggle DBOS and DSPy log verbosity for this process."""
        with self.lock:
            self.config = replace(self.config, verbose=verbose)
            self._apply_verbosity(verbose)

    def _apply_verbosity(self, verbose: bool) -> None:
        explicit = self.config.log_level
        logging.getLogger("dbos").setLevel(
            explicit or ("INFO" if verbose else "WARNING")
        )
        # DSPy's direct-forward warning is noise when Durable instruments a
        # module; hide it unless the caller asked for verbose output.
        logging.getLogger("dspy.primitives.module").setLevel(
            logging.WARNING if verbose else logging.ERROR
        )

    @property
    def name(self) -> str:
        return self.config.name

    def resolve_database_url(self) -> str:
        """Return the system database URL DBOS will use."""
        if self.config.database_url:
            return self.config.database_url
        # DBOS defaults to sqlite:///{name-with-underscores}.sqlite. Ask its
        # config processor rather than duplicating the naming rule.
        from dbos._dbos_config import process_config

        resolved = process_config(data={"name": self.config.name})
        return str(resolved["system_database_url"])

    def ensure_launched(self) -> None:
        """Launch DBOS on first use when this process owns the runtime."""
        load_dbos()
        if not self.config.manage_runtime:
            return
        with self.lock:
            if self.launched:
                return
            self._launch()

    def launch(self) -> None:
        """Explicitly launch the runtime (production servers)."""
        load_dbos()
        with self.lock:
            if self.launched:
                return
            self._launch()

    def _launch(self) -> None:
        from dbos import DBOS, DBOSConfig

        config: DBOSConfig = {"name": self.config.name}
        if self.config.database_url:
            config["system_database_url"] = self.config.database_url
        if self.config.application_version:
            config["application_version"] = self.config.application_version
        if self.config.executor_id:
            config["executor_id"] = self.config.executor_id
        if self.config.log_level:
            config["log_level"] = self.config.log_level
            config["console_log_level"] = self.config.log_level
        elif not self.config.verbose:
            config["log_level"] = "WARNING"
            config["console_log_level"] = "WARNING"
        config["serializer"] = self.serializer
        self._apply_verbosity(self.config.verbose)

        DBOS(config=config)
        DBOS.launch()
        self.launched = True
        # Queue registration needs a live system database. The queue manager
        # polls for newly registered queues, so registering after launch is
        # picked up within its polling interval.
        for queue_name, options in self.queues.items():
            DBOS.register_queue(queue_name, **options)

    def register_queue(self, name: str, **options: Any) -> None:
        """Declare a persistent queue before workers launch."""
        with self.lock:
            existing = self.queues.get(name)
            if existing is not None:
                if options and options != existing:
                    raise DurableError(
                        f"queue {name!r} was already declared with different options"
                    )
                return
            if self.launched:
                load_dbos().DBOS.register_queue(name, **options)
            self.queues[name] = options

    def client(self) -> Any:
        """Return a DBOS client for control-plane operations."""
        load_dbos()
        with self.lock:
            if self.client_instance is None:
                from dbos import DBOSClient

                self.client_instance = DBOSClient(
                    system_database_url=self.resolve_database_url(),
                    application_name=self.config.name,
                    serializer=self.serializer,
                )
            return self.client_instance

    def shutdown(self, *, timeout: float = 10.0) -> None:
        """Stop DBOS and release the client. Safe to call repeatedly.

        ``timeout`` is how long to let active workflows finish before the
        executor is torn down.
        """
        with self.lock:
            if self.client_instance is not None:
                try:
                    self.client_instance.destroy()
                finally:
                    self.client_instance = None
            if self.launched:
                load_dbos().DBOS.destroy(workflow_completion_timeout_sec=int(timeout))
                self.launched = False


_runtime = _Runtime()


def runtime() -> _Runtime:
    """Return the process-wide durable runtime."""
    return _runtime
