from sqlalchemy import Boolean, Column, Integer, String, Float, ForeignKey
from sqlalchemy.orm import relationship
from database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(String, default="Produtor Rural")

    # One-to-many: um usuário pode ser dono de várias fazendas (PR #3 — ownership).
    farms = relationship(
        "Farm",
        back_populates="owner",
        cascade="all, delete-orphan",
        foreign_keys="Farm.owner_id",
    )

class Farm(Base):
    __tablename__ = "farms"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    city = Column(String, nullable=False)
    total_area = Column(Float, nullable=False)
    latitude = Column(Float, default=-22.7182)
    longitude = Column(Float, default=-55.5421)

    # --- Ownership (PR #3) ---
    # FK para users.id. Nulo somente em bancos legados pré-migration;
    # o backfill de seed associa todo registro órfão ao admin demo.
    owner_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=True)
    # Fazenda "vitrine" compartilhada: legível por qualquer usuário autenticado
    # (ex.: farm demo id=1), mesmo não sendo o dono. Apenas o backend define
    # este flag (seed) — o payload de criação o ignora.
    is_shared = Column(Boolean, default=False, nullable=False)

    owner = relationship("User", back_populates="farms", foreign_keys=[owner_id])
    talhoes = relationship("Talhao", back_populates="farm", cascade="all, delete-orphan")

class Talhao(Base):
    __tablename__ = "talhoes"

    id = Column(Integer, primary_key=True, index=True)
    farm_id = Column(Integer, ForeignKey("farms.id"))
    name = Column(String, nullable=False)
    area = Column(Float, nullable=False)
    crop = Column(String, default="Soja / Milho Safrinha")
    latitude = Column(Float, default=-22.7182)
    longitude = Column(Float, default=-55.5421)
    kml_coordinates = Column(String, nullable=True)

    farm = relationship("Farm", back_populates="talhoes")