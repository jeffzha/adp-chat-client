from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy import UUID
from sqlalchemy.orm import Mapped, mapped_column

from model.base import Base


class WorkbenchSandboxAcceptanceEvent(Base):
    """Acceptance-only evidence for one provider Start side effect.

    ProviderInstanceId is retained solely so the authenticated acceptance cleanup
    endpoint can stop an instance whose successful Start response was deliberately
    discarded.  It must never be projected by an HTTP response or log message.
    """

    __tablename__ = "workbench_sandbox_acceptance_event"

    Id: Mapped[str] = mapped_column(
        UUID(), server_default=text("uuid_generate_v4()"), primary_key=True
    )
    AcceptanceRunId = Column(String(36), nullable=False, index=True)
    AccountId = Column(UUID(), nullable=False, index=True)
    BindingId = Column(String(64), nullable=False, index=True)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    NewApiUserId = Column(BigInteger(), nullable=False, index=True)
    ApplicationId = Column(String(64), nullable=False, index=True)
    AppProfileId = Column(String(64), nullable=False)
    ConfigVersion = Column(BigInteger(), nullable=False)
    AuthEpoch = Column(BigInteger(), nullable=False)
    ConversationId = Column(String(128), nullable=False, index=True)
    SandboxId = Column(String(64), nullable=False, index=True)
    Generation = Column(BigInteger(), nullable=False)
    ProviderInstanceId = Column(String(128), nullable=False)
    FaultMode = Column(String(64), nullable=False)
    CleanupStatus = Column(String(24), nullable=False, server_default="pending")
    CleanupAttempts = Column(Integer(), nullable=False, server_default="0")
    CleanupErrorCode = Column(String(64), nullable=True)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    UpdatedAt = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
