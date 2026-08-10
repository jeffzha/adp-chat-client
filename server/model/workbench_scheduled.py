from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import UUID
from sqlalchemy.orm import Mapped, mapped_column

from model.base import Base


class WorkbenchScheduledTask(Base):
    """Owner-scoped definition for a bounded offline workbench Turn."""

    __tablename__ = "workbench_scheduled_task"

    Id: Mapped[str] = mapped_column(
        UUID(), server_default=text("uuid_generate_v4()"), primary_key=True
    )
    TaskId = Column(String(64), nullable=False, unique=True, index=True)
    BindingId = Column(String(64), nullable=False, index=True)
    AccountId = Column(UUID(), nullable=False, index=True)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    NewApiUserId = Column(BigInteger(), nullable=False, index=True)
    CanonicalSubject = Column(String(255), nullable=False)
    ApplicationId = Column(String(64), nullable=False, index=True)
    ProviderAppId = Column(String(128), nullable=False)
    AppProfileId = Column(String(64), nullable=False, index=True)
    ConfigVersion = Column(BigInteger(), nullable=False)
    AuthEpoch = Column(BigInteger(), nullable=False)
    AgentId = Column(String(64), nullable=False)
    ConversationId = Column(UUID(), nullable=True, index=True)
    Name = Column(String(120), nullable=False)
    Prompt = Column(Text(), nullable=False)
    AttachmentIdsJson = Column(Text(), nullable=False, server_default="[]")
    ScheduleKind = Column(String(16), nullable=False)
    CronExpression = Column(String(128), nullable=True)
    Timezone = Column(String(64), nullable=False)
    OnceAt = Column(DateTime, nullable=True)
    NextRunAt = Column(DateTime, nullable=True, index=True)
    MisfirePolicy = Column(String(16), nullable=False)
    MaxRuntimeSeconds = Column(Integer(), nullable=False)
    DailyRunLimit = Column(Integer(), nullable=False)
    MaxRetries = Column(Integer(), nullable=False)
    RetryBackoffSeconds = Column(Integer(), nullable=False)
    PlanSnapshotJson = Column(Text(), nullable=False)
    PlanSnapshotHash = Column(String(64), nullable=False)
    Status = Column(String(16), nullable=False, index=True)
    Version = Column(BigInteger(), nullable=False, server_default="1")
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    UpdatedAt = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
    DeletedAt = Column(DateTime, nullable=True)


class WorkbenchScheduledDelegation(Base):
    """Versioned, revocable offline capability; plaintext is never persisted."""

    __tablename__ = "workbench_scheduled_delegation"
    __table_args__ = (
        UniqueConstraint(
            "TaskId",
            "CapabilityVersion",
            name="unique_workbench_scheduled_delegation_version",
        ),
    )

    Id: Mapped[str] = mapped_column(
        UUID(), server_default=text("uuid_generate_v4()"), primary_key=True
    )
    DelegationId = Column(String(64), nullable=False, unique=True, index=True)
    TaskId = Column(String(64), nullable=False, index=True)
    CapabilityVersion = Column(BigInteger(), nullable=False)
    CapabilityDigest = Column(String(64), nullable=False)
    AuthEpoch = Column(BigInteger(), nullable=False)
    ConfigVersion = Column(BigInteger(), nullable=False)
    PlanSnapshotHash = Column(String(64), nullable=False)
    Status = Column(String(16), nullable=False, index=True)
    ExpiresAt = Column(DateTime, nullable=False, index=True)
    RevokedAt = Column(DateTime, nullable=True)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())


class WorkbenchScheduledRun(Base):
    """One idempotent scheduled occurrence with a recoverable database lease."""

    __tablename__ = "workbench_scheduled_run"

    Id: Mapped[str] = mapped_column(
        UUID(), server_default=text("uuid_generate_v4()"), primary_key=True
    )
    RunId = Column(String(64), nullable=False, unique=True, index=True)
    TaskId = Column(String(64), nullable=False, index=True)
    IdempotencyKey = Column(String(64), nullable=False, unique=True, index=True)
    ScheduledFor = Column(DateTime, nullable=False, index=True)
    Status = Column(String(32), nullable=False, index=True)
    AttemptCount = Column(Integer(), nullable=False, server_default="0")
    NextAttemptAt = Column(DateTime, nullable=False, index=True)
    LeaseOwner = Column(String(128), nullable=True, index=True)
    LeaseUntil = Column(DateTime, nullable=True, index=True)
    TurnId = Column(String(64), nullable=True, index=True)
    ErrorCode = Column(String(64), nullable=True)
    ProviderStarted = Column(Boolean(), nullable=False, server_default=text("false"))
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    StartedAt = Column(DateTime, nullable=True)
    FinishedAt = Column(DateTime, nullable=True)
    UpdatedAt = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class WorkbenchScheduledAudit(Base):
    """Immutable, prompt-free security audit and local transactional outbox."""

    __tablename__ = "workbench_scheduled_audit"

    Id: Mapped[str] = mapped_column(
        UUID(), server_default=text("uuid_generate_v4()"), primary_key=True
    )
    EventId = Column(String(64), nullable=False, unique=True, index=True)
    TaskId = Column(String(64), nullable=False, index=True)
    RunId = Column(String(64), nullable=True, index=True)
    BindingId = Column(String(64), nullable=False, index=True)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    ApplicationId = Column(String(64), nullable=False, index=True)
    Action = Column(String(32), nullable=False, index=True)
    Result = Column(String(16), nullable=False)
    DetailCode = Column(String(64), nullable=True)
    OutboxStatus = Column(String(16), nullable=False, server_default="pending", index=True)
    LeaseOwner = Column(String(128), nullable=True)
    LeaseUntil = Column(DateTime, nullable=True)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    DeliveredAt = Column(DateTime, nullable=True)
