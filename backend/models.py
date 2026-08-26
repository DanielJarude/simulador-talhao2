from sqlalchemy import Column, Integer, String, Float, ForeignKey
from sqlalchemy.orm import relationship
from database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(String, default="Produtor Rural")

class Farm(Base):
    __tablename__ = "farms"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    city = Column(String, nullable=False)
    total_area = Column(Float, nullable=False)
    latitude = Column(Float, default=-22.7182)
    longitude = Column(Float, default=-55.5421)
    
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