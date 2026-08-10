from sqlalchemy import BigInteger, Column, DateTime, String, Text, UniqueConstraint, func, text
from sqlalchemy import UUID
from sqlalchemy.orm import Mapped, mapped_column

from model.base import Base


class WorkbenchIntegrationBinding(Base):
    """User consent and provider-sync state for one catalog resource."""

    __tablename__ = "workbench_integration_binding"
    __table_args__ = (
        UniqueConstraint(
            "BindingId",
            "ApplicationId",
            "AppProfileId",
            "ConfigVersion",
            "ResourceKind",
            "ResourceId",
            "ParentResourceId",
            name="uq_wb_integration_scope",
        ),
    )

    Id: Mapped[str] = mapped_column(
        UUID(), server_default=text("uuid_generate_v4()"), primary_key=True
    )
    IntegrationBindingId = Column(String(64), nullable=False, unique=True, index=True)
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
    ResourceKind = Column(String(16), nullable=False, index=True)
    ResourceId = Column(String(128), nullable=False, index=True)
    ParentResourceId = Column(String(128), nullable=False, server_default="")
    Status = Column(String(24), nullable=False, index=True)
    ProviderSyncStatus = Column(String(32), nullable=False, index=True)
    UserConsentedAt = Column(DateTime, nullable=False)
    RevokedAt = Column(DateTime, nullable=True)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    UpdatedAt = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class WorkbenchConnectorCredential(Base):
    """JWE-encrypted OAuth tokens scoped to one complete workbench identity."""

    __tablename__ = "workbench_connector_credential"
    __table_args__ = (
        UniqueConstraint(
            "BindingId",
            "ApplicationId",
            "AppProfileId",
            "ConfigVersion",
            "ProviderId",
            "ConnectorId",
            name="uq_wb_connector_scope",
        ),
    )

    Id: Mapped[str] = mapped_column(
        UUID(), server_default=text("uuid_generate_v4()"), primary_key=True
    )
    CredentialId = Column(String(64), nullable=False, unique=True, index=True)
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
    ProviderId = Column(String(64), nullable=False, index=True)
    ConnectorId = Column(String(128), nullable=False, index=True)
    TokenCiphertext = Column(Text(), nullable=False)
    EncryptionKeyId = Column(String(64), nullable=False)
    TokenDigest = Column(String(64), nullable=False)
    GrantedScopesJson = Column(Text(), nullable=False)
    TokenType = Column(String(32), nullable=False)
    ExpiresAt = Column(DateTime, nullable=True)
    Status = Column(String(24), nullable=False, index=True)
    RevocationStatus = Column(String(32), nullable=False)
    ConnectedAt = Column(DateTime, nullable=False)
    RevokedAt = Column(DateTime, nullable=True)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    UpdatedAt = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class WorkbenchConnectorScope(Base):
    """Monotonic connector generation for one complete user/App scope."""

    __tablename__ = "workbench_connector_scope"
    __table_args__ = (
        UniqueConstraint(
            "BindingId",
            "ApplicationId",
            "AppProfileId",
            "ConfigVersion",
            "ProviderId",
            "ConnectorId",
            name="uq_wb_connector_generation_scope",
        ),
    )

    Id: Mapped[str] = mapped_column(
        UUID(), server_default=text("uuid_generate_v4()"), primary_key=True
    )
    ScopeId = Column(String(64), nullable=False, unique=True, index=True)
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
    ProviderId = Column(String(64), nullable=False, index=True)
    ConnectorId = Column(String(128), nullable=False, index=True)
    Generation = Column(BigInteger(), nullable=False, server_default="0")
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    UpdatedAt = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class WorkbenchOAuthState(Base):
    """One-time OAuth transaction; only state/nonce digests are persisted."""

    # V2 is intentionally a new table. Existing pending rows lack browser-session
    # binding and a connector generation and therefore must never be completed.
    __tablename__ = "workbench_oauth_state_v2"

    Id: Mapped[str] = mapped_column(
        UUID(), server_default=text("uuid_generate_v4()"), primary_key=True
    )
    OAuthStateId = Column(String(64), nullable=False, unique=True, index=True)
    StateDigest = Column(String(64), nullable=False, unique=True, index=True)
    NonceDigest = Column(String(64), nullable=False)
    BrowserCookieDigest = Column(String(64), nullable=False)
    SessionIdDigest = Column(String(64), nullable=False)
    VerifierCiphertext = Column(Text(), nullable=False)
    EncryptionKeyId = Column(String(64), nullable=False)
    BindingId = Column(String(64), nullable=False, index=True)
    AccountId = Column(UUID(), nullable=False, index=True)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    NewApiUserId = Column(BigInteger(), nullable=False)
    CanonicalSubject = Column(String(255), nullable=False)
    ApplicationId = Column(String(64), nullable=False, index=True)
    ProviderAppId = Column(String(128), nullable=False)
    AppProfileId = Column(String(64), nullable=False)
    ConfigVersion = Column(BigInteger(), nullable=False)
    AuthEpoch = Column(BigInteger(), nullable=False)
    ProviderId = Column(String(64), nullable=False)
    ConnectorId = Column(String(128), nullable=False)
    ConnectionGeneration = Column(BigInteger(), nullable=False)
    RedirectUri = Column(String(1024), nullable=False)
    RequestedScopesJson = Column(Text(), nullable=False)
    Status = Column(String(24), nullable=False, index=True)
    ExpiresAt = Column(DateTime, nullable=False, index=True)
    ConsumedAt = Column(DateTime, nullable=True)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())


class WorkbenchOAuthRevocation(Base):
    """Encrypted, durable compensation for provider tokens that must be revoked."""

    __tablename__ = "workbench_oauth_revocation"

    Id: Mapped[str] = mapped_column(
        UUID(), server_default=text("uuid_generate_v4()"), primary_key=True
    )
    RevocationId = Column(String(64), nullable=False, unique=True, index=True)
    BindingId = Column(String(64), nullable=False, index=True)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    ApplicationId = Column(String(64), nullable=False, index=True)
    CredentialId = Column(String(64), nullable=False, server_default="")
    ProviderId = Column(String(64), nullable=False, index=True)
    TokenCiphertext = Column(Text(), nullable=False)
    EncryptionKeyId = Column(String(64), nullable=False)
    TokenDigest = Column(String(64), nullable=False, index=True)
    Status = Column(String(24), nullable=False, index=True)
    AttemptCount = Column(BigInteger(), nullable=False, server_default="0")
    AvailableAt = Column(DateTime, nullable=False, index=True)
    LeaseExpiresAt = Column(DateTime, nullable=True, index=True)
    LastErrorCode = Column(String(64), nullable=False, server_default="")
    CompletedAt = Column(DateTime, nullable=True)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    UpdatedAt = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class WorkbenchIntegrationAudit(Base):
    """Prompt/token-free integration security audit record."""

    __tablename__ = "workbench_integration_audit"

    Id: Mapped[str] = mapped_column(
        UUID(), server_default=text("uuid_generate_v4()"), primary_key=True
    )
    AuditId = Column(String(64), nullable=False, unique=True, index=True)
    BindingId = Column(String(64), nullable=False, index=True)
    AccountId = Column(UUID(), nullable=False, index=True)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    ApplicationId = Column(String(64), nullable=False, index=True)
    AppProfileId = Column(String(64), nullable=False)
    ConfigVersion = Column(BigInteger(), nullable=False)
    Action = Column(String(64), nullable=False, index=True)
    ResourceKind = Column(String(16), nullable=False)
    ResourceId = Column(String(128), nullable=False)
    Outcome = Column(String(24), nullable=False, index=True)
    ErrorCode = Column(String(64), nullable=False, server_default="")
    MetadataJson = Column(Text(), nullable=False, server_default="{}")
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
