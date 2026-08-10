from sqlalchemy import (
    Boolean,
    BigInteger,
    Column,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import UUID
from sqlalchemy.orm import Mapped, mapped_column

from model.base import Base


class WorkbenchSandbox(Base):
    """Owner-scoped handle for one managed provider sandbox.

    Provider credentials, acquired access tokens, and direct-connect URLs are
    deliberately absent.  ClientTokenHash is evidence that the deterministic
    token used by competing blue/green processes was the same; it is not the
    provider token itself.
    """

    __tablename__ = "workbench_sandbox"
    __table_args__ = (
        UniqueConstraint(
            "CustomerId",
            "NewApiUserId",
            "ApplicationId",
            "ConversationId",
            name="unique_workbench_sandbox_owner_conversation",
        ),
    )

    Id: Mapped[str] = mapped_column(
        UUID(), server_default=text("uuid_generate_v4()"), primary_key=True
    )
    SandboxId = Column(String(64), nullable=False, unique=True, index=True)
    BindingId = Column(String(64), nullable=False, index=True)
    AccountId = Column(UUID(), nullable=False, index=True)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    NewApiUserId = Column(BigInteger(), nullable=False, index=True)
    ApplicationId = Column(String(64), nullable=False, index=True)
    AppProfileId = Column(String(64), nullable=False)
    ConfigVersion = Column(BigInteger(), nullable=False)
    AuthEpoch = Column(BigInteger(), nullable=False)
    ConversationId = Column(String(128), nullable=False, index=True)
    Provider = Column(String(32), nullable=False)
    ProviderInstanceId = Column(String(128), nullable=True, unique=True)
    ClientTokenHash = Column(String(64), nullable=False)
    Generation = Column(BigInteger(), nullable=False)
    Version = Column(BigInteger(), nullable=False, server_default="1")
    Status = Column(String(24), nullable=False, index=True)
    TimeoutSeconds = Column(Integer(), nullable=False)
    LeaseOwner = Column(String(128), nullable=True, index=True)
    LeaseUntil = Column(DateTime, nullable=True, index=True)
    ExpiresAt = Column(DateTime, nullable=True)
    ErrorCode = Column(String(64), nullable=True)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    UpdatedAt = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class WorkbenchSandboxAudit(Base):
    """Secret-free security and lifecycle evidence for managed sandboxes."""

    __tablename__ = "workbench_sandbox_audit"

    Id: Mapped[str] = mapped_column(
        UUID(), server_default=text("uuid_generate_v4()"), primary_key=True
    )
    SandboxId = Column(String(64), nullable=False, index=True)
    BindingId = Column(String(64), nullable=False, index=True)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    NewApiUserId = Column(BigInteger(), nullable=False, index=True)
    ApplicationId = Column(String(64), nullable=False, index=True)
    ConversationId = Column(String(128), nullable=False, index=True)
    EventType = Column(String(64), nullable=False, index=True)
    Outcome = Column(String(32), nullable=False)
    StatusFrom = Column(String(24), nullable=True)
    StatusTo = Column(String(24), nullable=True)
    ErrorCode = Column(String(64), nullable=True)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())


class WorkbenchSandboxPty(Base):
    """Durable, secret-free owner snapshot for one browser PTY attempt.

    Ticket material is never stored: only its SHA-256 digest is persisted.
    Provider locators and the remote PID are intentionally admin-internal and
    never included in public projections or logs.
    """

    __tablename__ = "workbench_sandbox_pty"

    Id: Mapped[str] = mapped_column(
        UUID(), server_default=text("uuid_generate_v4()"), primary_key=True
    )
    PtySessionId = Column(String(64), nullable=False, unique=True, index=True)
    SandboxId = Column(String(64), nullable=False, index=True)
    SandboxGeneration = Column(BigInteger(), nullable=False)
    ProviderInstanceId = Column(String(128), nullable=False)
    ProviderPid = Column(BigInteger(), nullable=True)
    BindingId = Column(String(64), nullable=False, index=True)
    AccountId = Column(UUID(), nullable=False, index=True)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    NewApiUserId = Column(BigInteger(), nullable=False, index=True)
    CanonicalSubject = Column(String(255), nullable=False)
    ApplicationId = Column(String(64), nullable=False, index=True)
    AppProfileId = Column(String(64), nullable=False)
    ConfigVersion = Column(BigInteger(), nullable=False)
    AuthEpoch = Column(BigInteger(), nullable=False)
    ConversationId = Column(String(128), nullable=False, index=True)
    TicketHash = Column(String(64), nullable=False, unique=True, index=True)
    TicketExpiresAt = Column(DateTime, nullable=False, index=True)
    TicketConsumedAt = Column(DateTime, nullable=True)
    Status = Column(String(24), nullable=False, index=True)
    Rows = Column(Integer(), nullable=False)
    Cols = Column(Integer(), nullable=False)
    TimeoutSeconds = Column(Integer(), nullable=False)
    DeadlineAt = Column(DateTime, nullable=False, index=True)
    LastHeartbeatAt = Column(DateTime, nullable=False, index=True)
    LeaseOwner = Column(String(128), nullable=True, index=True)
    LeaseUntil = Column(DateTime, nullable=True, index=True)
    InputBytes = Column(BigInteger(), nullable=False, server_default="0")
    OutputBytes = Column(BigInteger(), nullable=False, server_default="0")
    InputFrames = Column(BigInteger(), nullable=False, server_default="0")
    OutputFrames = Column(BigInteger(), nullable=False, server_default="0")
    NormalClose = Column(Boolean(), nullable=False, server_default="false")
    ErrorCode = Column(String(64), nullable=True)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    UpdatedAt = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
    ClosedAt = Column(DateTime, nullable=True)
