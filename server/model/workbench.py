from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy import UUID
from sqlalchemy.orm import Mapped, mapped_column

from model.base import Base


class WorkbenchIdentity(Base):
    __tablename__ = "workbench_identity"
    __table_args__ = (
        UniqueConstraint("CustomerId", "NewApiUserId", name="unique_workbench_customer_user"),
    )

    Id: Mapped[str] = mapped_column(
        UUID(),
        server_default=text("uuid_generate_v4()"),
        primary_key=True,
    )
    BindingId = Column(String(64), nullable=False, unique=True)
    CanonicalSubject = Column(String(255), nullable=False, unique=True)
    AccountId = Column(UUID(), nullable=False, unique=True, index=True)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    NewApiUserId = Column(BigInteger(), nullable=False, index=True)
    AuthEpoch = Column(BigInteger(), nullable=False, server_default="0")
    Status = Column(String(16), nullable=False, server_default="active")
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    UpdatedAt = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
    LastAuthenticatedAt = Column(DateTime, nullable=True)


class WorkbenchBrowserSession(Base):
    """Server-side record for one signed workbench browser session."""

    __tablename__ = "workbench_browser_session"

    Id: Mapped[str] = mapped_column(
        UUID(),
        server_default=text("uuid_generate_v4()"),
        primary_key=True,
    )
    SessionIdDigest = Column(String(64), nullable=False, unique=True, index=True)
    BindingId = Column(String(64), nullable=False, index=True)
    AccountId = Column(UUID(), nullable=False, index=True)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    NewApiUserId = Column(BigInteger(), nullable=False, index=True)
    CanonicalSubject = Column(String(255), nullable=False)
    AuthEpoch = Column(BigInteger(), nullable=False)
    ApplicationId = Column(String(64), nullable=False, index=True)
    AppProfileId = Column(String(64), nullable=False)
    ConfigVersion = Column(BigInteger(), nullable=False)
    AuthenticatedAt = Column(DateTime, nullable=False, index=True)
    ExpiresAt = Column(DateTime, nullable=False, index=True)
    Status = Column(String(16), nullable=False, index=True)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())


class WorkbenchAgentBinding(Base):
    __tablename__ = "workbench_agent_binding"
    __table_args__ = (
        UniqueConstraint("BindingId", "ApplicationId", name="unique_workbench_binding_application"),
        UniqueConstraint("AccountId", "ApplicationId", name="unique_workbench_account_application"),
    )

    Id: Mapped[str] = mapped_column(
        UUID(),
        server_default=text("uuid_generate_v4()"),
        primary_key=True,
    )
    BindingId = Column(String(64), nullable=False, index=True)
    AccountId = Column(UUID(), nullable=False, index=True)
    ApplicationId = Column(String(64), nullable=False, index=True)
    AppProfileId = Column(String(64), nullable=True, index=True)
    ConfigVersion = Column(BigInteger(), nullable=True)
    ConfigFingerprint = Column(String(80), nullable=True)
    MigrationMemberId = Column(String(64), nullable=True, unique=True, index=True)
    AgentId = Column(String(64), nullable=True)
    Status = Column(String(32), nullable=False)
    AttemptId = Column(String(64), nullable=False)
    ErrorCode = Column(String(64), nullable=True)
    LimitFingerprint = Column(String(64), nullable=True)
    LimitsVerifiedAt = Column(DateTime, nullable=True)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    UpdatedAt = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class WorkbenchAppLineage(Base):
    """Signed cutover-activated, immutable history-only App tuple."""

    __tablename__ = "workbench_app_lineage"
    __table_args__ = (
        UniqueConstraint(
            "SourceAppProfileId",
            "TargetAppProfileId",
            name="unique_workbench_app_lineage_tuple",
        ),
    )

    Id: Mapped[str] = mapped_column(
        UUID(),
        server_default=text("uuid_generate_v4()"),
        primary_key=True,
    )
    LineageId = Column(String(64), nullable=False, unique=True, index=True)
    EventKey = Column(String(160), nullable=False, unique=True, index=True)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    MigrationJobId = Column(String(64), nullable=False, unique=True, index=True)
    SourceApplicationId = Column(String(64), nullable=False, index=True)
    SourceProviderAppId = Column(String(128), nullable=False)
    SourceAppProfileId = Column(BigInteger(), nullable=False, index=True)
    SourceConfigVersion = Column(BigInteger(), nullable=False)
    TargetApplicationId = Column(String(64), nullable=False, index=True)
    TargetProviderAppId = Column(String(128), nullable=False)
    TargetAppProfileId = Column(BigInteger(), nullable=False, index=True)
    TargetConfigVersion = Column(BigInteger(), nullable=False)
    MigrationConfigFingerprint = Column(String(80), nullable=False)
    Status = Column(String(16), nullable=False)
    ActivatedAt = Column(DateTime, nullable=False, index=True)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())


class WorkbenchRuntimeLease(Base):
    """Recoverable, cross-instance guard for workbench operations.

    A lease is intentionally database-backed.  ADP blue and green processes share
    PostgreSQL, so an in-process semaphore would not protect the customer limit.
    """

    __tablename__ = "workbench_runtime_lease"

    Id: Mapped[str] = mapped_column(
        UUID(),
        server_default=text("uuid_generate_v4()"),
        primary_key=True,
    )
    LeaseId = Column(String(64), nullable=False, unique=True, index=True)
    BindingId = Column(String(64), nullable=False, index=True)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    AccountId = Column(UUID(), nullable=False, index=True)
    ApplicationId = Column(String(64), nullable=False, index=True)
    Operation = Column(String(32), nullable=False)
    MaxRuntimeSeconds = Column(Integer(), nullable=False)
    AcquiredAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    ExpiresAt = Column(DateTime, nullable=False, index=True)


class WorkbenchFileBinding(Base):
    """Ownership proof for a file uploaded through the workbench proxy.

    Locator digests provide tamper evidence.  LocatorCiphertext is never returned
    by an API; it lets the server rebuild a provider request from an opaque
    WorkbenchFileId without storing a bearer URL in plaintext.
    """

    __tablename__ = "workbench_file_binding"

    Id: Mapped[str] = mapped_column(
        UUID(),
        server_default=text("uuid_generate_v4()"),
        primary_key=True,
    )
    FileId = Column(String(64), nullable=False, unique=True, index=True)
    BindingId = Column(String(64), nullable=False, index=True)
    AccountId = Column(UUID(), nullable=False, index=True)
    ApplicationId = Column(String(64), nullable=False, index=True)
    ProviderAppId = Column(String(128), nullable=False)
    FileName = Column(String(255), nullable=False)
    FileType = Column(String(128), nullable=False)
    FileUrlHash = Column(String(64), nullable=True, index=True)
    CosUrlHash = Column(String(64), nullable=True, index=True)
    CosBucketHash = Column(String(64), nullable=True)
    LocatorCiphertext = Column(Text(), nullable=False)
    FileSize = Column(BigInteger(), nullable=False)
    Status = Column(String(16), nullable=False, server_default="active")
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())


class WorkbenchWorkspace(Base):
    """Local ownership boundary for exactly one owned Conversation.

    Provider workspace locators are optional and may only be persisted as JWE
    ciphertext.  WorkspaceId is the opaque local handle used by ownership
    checks; it is never a Tencent provider workspace identifier.
    """

    __tablename__ = "workbench_workspace"
    __table_args__ = (
        UniqueConstraint(
            "ConversationId",
            "BindingId",
            "AccountId",
            "CustomerId",
            "ApplicationId",
            "AppProfileId",
            "ConfigVersion",
            name="unique_workbench_conversation_workspace_scope",
        ),
    )

    Id: Mapped[str] = mapped_column(
        UUID(),
        server_default=text("uuid_generate_v4()"),
        primary_key=True,
    )
    WorkspaceId = Column(String(64), nullable=False, unique=True, index=True)
    ConversationId = Column(UUID(), nullable=False, unique=True, index=True)
    BindingId = Column(String(64), nullable=False, index=True)
    AccountId = Column(UUID(), nullable=False, index=True)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    ApplicationId = Column(String(64), nullable=False, index=True)
    ProviderAppId = Column(String(128), nullable=False)
    AppProfileId = Column(String(64), nullable=False, index=True)
    ConfigVersion = Column(BigInteger(), nullable=False)
    Kind = Column(String(16), nullable=False, server_default="conversation")
    ProviderLocatorCiphertext = Column(Text(), nullable=True)
    ProviderLocatorKeyId = Column(String(64), nullable=True)
    Status = Column(String(16), nullable=False, server_default="pending", index=True)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    UpdatedAt = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class WorkbenchConversationWorkspace(Base):
    """Non-invasive Conversation -> owned local Workspace mapping."""

    __tablename__ = "workbench_conversation_workspace"

    Id: Mapped[str] = mapped_column(
        UUID(),
        server_default=text("uuid_generate_v4()"),
        primary_key=True,
    )
    ConversationId = Column(UUID(), nullable=False, unique=True, index=True)
    # Files uploaded before the first Turn are account-owned and intentionally
    # unassigned. The first explicit existing-Conversation use binds this once.
    WorkspaceId = Column(String(64), nullable=True, index=True)
    BindingId = Column(String(64), nullable=False, index=True)
    AccountId = Column(UUID(), nullable=False, index=True)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    ApplicationId = Column(String(64), nullable=False, index=True)
    ProviderAppId = Column(String(128), nullable=False)
    AppProfileId = Column(String(64), nullable=False, index=True)
    ConfigVersion = Column(BigInteger(), nullable=False)
    AgentId = Column(String(64), nullable=False)
    Status = Column(String(16), nullable=False, server_default="pending", index=True)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    UpdatedAt = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class WorkbenchFileWorkspace(Base):
    """Defense-in-depth ownership scope for an opaque WorkbenchFileId."""

    __tablename__ = "workbench_file_workspace"

    Id: Mapped[str] = mapped_column(
        UUID(),
        server_default=text("uuid_generate_v4()"),
        primary_key=True,
    )
    FileId = Column(String(64), nullable=False, unique=True, index=True)
    WorkspaceId = Column(String(64), nullable=True, index=True)
    BindingId = Column(String(64), nullable=False, index=True)
    AccountId = Column(UUID(), nullable=False, index=True)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    ApplicationId = Column(String(64), nullable=False, index=True)
    ProviderAppId = Column(String(128), nullable=False)
    AppProfileId = Column(String(64), nullable=False, index=True)
    ConfigVersion = Column(BigInteger(), nullable=False)
    Status = Column(String(16), nullable=False, server_default="pending", index=True)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    UpdatedAt = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class WorkbenchResourceOutbox(Base):
    """Durable, idempotent control-plane resource ownership report."""

    __tablename__ = "workbench_resource_outbox"

    Id: Mapped[str] = mapped_column(
        UUID(),
        server_default=text("uuid_generate_v4()"),
        primary_key=True,
    )
    EventId = Column(String(64), nullable=False, unique=True, index=True)
    ResourceKeyHash = Column(String(64), nullable=False, unique=True, index=True)
    BindingId = Column(String(64), nullable=False, index=True)
    CanonicalSubject = Column(String(255), nullable=False)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    ApplicationId = Column(String(128), nullable=False, index=True)
    AppProfileId = Column(BigInteger(), nullable=False, index=True)
    ConfigVersion = Column(BigInteger(), nullable=False)
    ResourceType = Column(String(32), nullable=False, index=True)
    ResourceId = Column(String(255), nullable=False)
    ParentResourceType = Column(String(32), nullable=False, server_default="")
    ParentResourceId = Column(String(255), nullable=False, server_default="")
    SourceVersion = Column(BigInteger(), nullable=False, server_default="1")
    Status = Column(String(16), nullable=False, server_default="pending", index=True)
    AttemptCount = Column(Integer(), nullable=False, server_default="0")
    NextAttemptAt = Column(DateTime, nullable=False, server_default=func.current_timestamp(), index=True)
    LeaseUntil = Column(DateTime, nullable=True, index=True)
    LastError = Column(String(255), nullable=True)
    ControlBindingId = Column(String(64), nullable=True)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    UpdatedAt = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
    DeliveredAt = Column(DateTime, nullable=True)


class WorkbenchTurn(Base):
    """Durable identity and lifecycle for one idempotent workbench Turn."""

    __tablename__ = "workbench_turn"
    __table_args__ = (
        UniqueConstraint(
            "BindingId",
            "ApplicationId",
            "ClientRequestId",
            name="unique_workbench_turn_request",
        ),
    )

    Id: Mapped[str] = mapped_column(
        UUID(),
        server_default=text("uuid_generate_v4()"),
        primary_key=True,
    )
    TurnId = Column(String(64), nullable=False, unique=True, index=True)
    BindingId = Column(String(64), nullable=False, index=True)
    AccountId = Column(UUID(), nullable=False, index=True)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    ApplicationId = Column(String(64), nullable=False, index=True)
    ClientRequestId = Column(String(64), nullable=False)
    RequestDigest = Column(String(64), nullable=False)
    Status = Column(String(32), nullable=False, index=True)
    ConversationId = Column(String(64), nullable=True, index=True)
    OwnerInstanceId = Column(String(128), nullable=False, index=True)
    OwnerLifecycleId = Column(String(64), nullable=False, index=True)
    EventCount = Column(Integer(), nullable=False, server_default="0")
    EventBytes = Column(BigInteger(), nullable=False, server_default="0")
    ErrorCode = Column(String(64), nullable=True)
    AcceptedAt = Column(DateTime, nullable=True)
    CompletedAt = Column(DateTime, nullable=True)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    UpdatedAt = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class WorkbenchTurnEvent(Base):
    """Bounded ordered SSE event persisted for replay and reconnection."""

    __tablename__ = "workbench_turn_event"
    __table_args__ = (
        UniqueConstraint("TurnId", "Sequence", name="unique_workbench_turn_event_sequence"),
    )

    Id: Mapped[str] = mapped_column(
        UUID(),
        server_default=text("uuid_generate_v4()"),
        primary_key=True,
    )
    TurnId = Column(String(64), nullable=False, index=True)
    Sequence = Column(BigInteger(), nullable=False)
    EventData = Column(Text(), nullable=False)
    EventBytes = Column(Integer(), nullable=False)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())


class WorkbenchTurnEvidence(Base):
    """Encrypted, immutable provider completion evidence for one Turn."""

    __tablename__ = "workbench_turn_evidence"

    Id: Mapped[str] = mapped_column(
        UUID(),
        server_default=text("uuid_generate_v4()"),
        primary_key=True,
    )
    EvidenceId = Column(String(64), nullable=False, unique=True, index=True)
    TurnId = Column(String(64), nullable=False, unique=True, index=True)
    BindingId = Column(String(64), nullable=False, index=True)
    AccountId = Column(UUID(), nullable=False, index=True)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    ApplicationId = Column(String(64), nullable=False, index=True)
    AppProfileId = Column(String(64), nullable=False)
    ConfigVersion = Column(BigInteger(), nullable=False)
    Source = Column(String(64), nullable=False)
    EventType = Column(String(64), nullable=False)
    EvidenceSha256 = Column(String(64), nullable=False, index=True)
    EvidenceCiphertext = Column(Text(), nullable=False)
    EncryptionKeyId = Column(String(64), nullable=False)
    ProviderRequestId = Column(String(255), nullable=True, index=True)
    ProviderTraceId = Column(String(255), nullable=True, index=True)
    ProviderRecordId = Column(String(255), nullable=True, index=True)
    ConversationId = Column(String(255), nullable=True, index=True)
    DedupeConfidence = Column(String(16), nullable=False)
    ObservedAt = Column(DateTime, nullable=False)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())


class WorkbenchTurnUsageDatum(Base):
    """Non-additive usage projection tied to encrypted source evidence."""

    __tablename__ = "workbench_turn_usage_datum"
    __table_args__ = (
        UniqueConstraint(
            "EvidenceId",
            "SourcePath",
            name="unique_workbench_turn_usage_source_path",
        ),
    )

    Id: Mapped[str] = mapped_column(
        UUID(),
        server_default=text("uuid_generate_v4()"),
        primary_key=True,
    )
    UsageId = Column(String(64), nullable=False, unique=True, index=True)
    EvidenceId = Column(String(64), nullable=False, index=True)
    TurnId = Column(String(64), nullable=False, index=True)
    Source = Column(String(64), nullable=False)
    SourcePath = Column(String(512), nullable=False)
    EvidenceSha256 = Column(String(64), nullable=False, index=True)
    StableUsageKey = Column(String(64), nullable=True, index=True)
    DedupeConfidence = Column(String(16), nullable=False)
    MetricsJson = Column(Text(), nullable=False)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())


class WorkbenchTurnCancellation(Base):
    """Persistent local cancellation intent and optional provider confirmation."""

    __tablename__ = "workbench_turn_cancellation"

    Id: Mapped[str] = mapped_column(
        UUID(),
        server_default=text("uuid_generate_v4()"),
        primary_key=True,
    )
    CancellationId = Column(String(64), nullable=False, unique=True, index=True)
    TurnId = Column(String(64), nullable=False, unique=True, index=True)
    BindingId = Column(String(64), nullable=False, index=True)
    AccountId = Column(UUID(), nullable=False, index=True)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    ApplicationId = Column(String(64), nullable=False, index=True)
    Status = Column(String(32), nullable=False, index=True)
    ReasonCode = Column(String(32), nullable=False)
    ProviderEvidenceSha256 = Column(String(64), nullable=True)
    RequestedAt = Column(DateTime, nullable=False)
    ConfirmedAt = Column(DateTime, nullable=True)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    UpdatedAt = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
