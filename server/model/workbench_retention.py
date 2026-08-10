from sqlalchemy import BigInteger, Column, DateTime, String, Text, UniqueConstraint, func, text
from sqlalchemy import UUID
from sqlalchemy.orm import Mapped, mapped_column

from model.base import Base


class WorkbenchRetentionReceipt(Base):
    """Secret-free, idempotent audit receipt for one control-plane intent."""

    __tablename__ = "workbench_retention_receipt"

    Id: Mapped[str] = mapped_column(
        UUID(), server_default=text("uuid_generate_v4()"), primary_key=True
    )
    ReceiptId = Column(String(64), nullable=False, unique=True, index=True)
    IntentId = Column(String(64), nullable=False, unique=True, index=True)
    RequestDigest = Column(String(64), nullable=False)
    CustomerId = Column(BigInteger(), nullable=False, index=True)
    PolicyVersion = Column(BigInteger(), nullable=False)
    CutoffAt = Column(DateTime, nullable=False)
    Status = Column(String(24), nullable=False, index=True)
    CountsJson = Column(Text(), nullable=False)
    CompletedAt = Column(DateTime, nullable=False)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())


class WorkbenchInboundNonce(Base):
    """Replay barrier for signed claw-control to ADP internal requests."""

    __tablename__ = "workbench_inbound_nonce"
    __table_args__ = (
        UniqueConstraint("ServiceName", "Nonce", name="uq_workbench_inbound_nonce"),
    )

    Id: Mapped[str] = mapped_column(
        UUID(), server_default=text("uuid_generate_v4()"), primary_key=True
    )
    ServiceName = Column(String(48), nullable=False, index=True)
    Nonce = Column(String(96), nullable=False)
    ExpiresAt = Column(DateTime, nullable=False, index=True)
    CreatedAt = Column(DateTime, nullable=False, server_default=func.current_timestamp())
