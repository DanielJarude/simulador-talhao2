// app.js - Camada de Integração Front-end <-> Back-end FastAPI (Orion Agro)
const API_URL = "http://localhost:8000/api";

/**
 * SERVIÇO DE AUTENTICAÇÃO
 */
const AuthService = {
  // Login de Usuário
  login: async (email, password) => {
    try {
      const res = await fetch(`${API_URL}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password })
      });
      if (!res.ok) {
        const errData = await res.json();
        throw new Error(errData.detail || "E-mail ou senha incorretos.");
      }
      const user = await res.json();
      localStorage.setItem("orion_auth_user", JSON.stringify(user));
      return { success: true, user };
    } catch (e) {
      return { success: false, message: e.message };
    }
  },

  // Cadastro de Novo Usuário
  register: async (name, email, password, role) => {
    try {
      const res = await fetch(`${API_URL}/auth/register`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, email, password, role })
      });
      if (!res.ok) {
        const errData = await res.json();
        throw new Error(errData.detail || "Erro ao cadastrar usuário.");
      }
      const user = await res.json();
      localStorage.setItem("orion_auth_user", JSON.stringify(user));
      return { success: true, user };
    } catch (e) {
      return { success: false, message: e.message };
    }
  },

  // Recupera usuário ativo da sessão
  getCurrentUser: () => {
    try {
      return JSON.parse(localStorage.getItem("orion_auth_user"));
    } catch {
      return null;
    }
  },

  // Encerra a sessão
  logout: () => {
    localStorage.removeItem("orion_auth_user");
  }
};

/**
 * SERVIÇO DE GESTÃO DE PROPRIEDADES & TALHÕES (CRUD COMPLETO)
 */
const FarmService = {
  // Listar todas as fazendas cadastradas
  getFarms: async () => {
    try {
      const res = await fetch(`${API_URL}/farms`);
      if (!res.ok) throw new Error("Falha ao buscar lista de propriedades.");
      return await res.json();
    } catch (e) {
      console.error("Erro no FarmService.getFarms:", e);
      throw e;
    }
  },

  // Buscar detalhes de uma fazenda por ID
  getFarmById: async (id) => {
    try {
      const res = await fetch(`${API_URL}/farms/${id}`);
      if (res.ok) {
        return await res.json();
      }
      throw new Error("Propriedade não encontrada.");
    } catch (e) {
      console.warn(`Erro ao buscar fazenda ID ${id}:`, e);
      return null;
    }
  },

  // Cadastrar nova fazenda com coordenadas geográficas e contorno KML/GeoJSON
  createFarm: async (name, city, totalArea, talhaoName, crop, lat, lon, kmlCoordinates = null) => {
    try {
      const res = await fetch(`${API_URL}/farms`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name.trim(),
          city: city.trim(),
          total_area: parseFloat(totalArea),
          talhao_name: talhaoName.trim(),
          crop: crop,
          latitude: parseFloat(lat) || -22.7182,
          longitude: parseFloat(lon) || -55.5421,
          kml_coordinates: kmlCoordinates || null
        })
      });

      if (!res.ok) {
        const errData = await res.json();
        throw new Error(errData.detail || "Erro ao salvar propriedade no servidor.");
      }

      return await res.json();
    } catch (e) {
      console.error("Erro no FarmService.createFarm:", e);
      throw e;
    }
  },

  // Atualizar dados de uma fazenda existente
  updateFarm: async (id, name, city, totalArea, talhaoName, crop, lat, lon, kmlCoordinates = null) => {
    try {
      const res = await fetch(`${API_URL}/farms/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name.trim(),
          city: city.trim(),
          total_area: parseFloat(totalArea),
          talhao_name: talhaoName.trim(),
          crop: crop,
          latitude: parseFloat(lat) || -22.7182,
          longitude: parseFloat(lon) || -55.5421,
          kml_coordinates: kmlCoordinates || null
        })
      });

      if (!res.ok) {
        const errData = await res.json();
        throw new Error(errData.detail || "Erro ao atualizar propriedade no servidor.");
      }

      return await res.json();
    } catch (e) {
      console.error("Erro no FarmService.updateFarm:", e);
      throw e;
    }
  },

  // Excluir fazenda e seus talhões vinculados
  deleteFarm: async (id) => {
    try {
      const res = await fetch(`${API_URL}/farms/${id}`, {
        method: "DELETE"
      });

      if (!res.ok) {
        const errData = await res.json();
        throw new Error(errData.detail || "Erro ao excluir propriedade no servidor.");
      }

      return await res.json();
    } catch (e) {
      console.error("Erro no FarmService.deleteFarm:", e);
      throw e;
    }
  }
};

/**
 * SERVIÇO DE CLIMA & SATÉLITE
 */
const WeatherService = {
  // Obter clima diário e histórico da NASA POWER ao vivo por ID da fazenda
  getLiveWeather: async (farmId) => {
    try {
      const res = await fetch(`${API_URL}/weather/farm/${farmId}`);
      if (!res.ok) throw new Error("Erro ao consultar dados meteorológicos.");
      return await res.json();
    } catch (e) {
      console.warn("Erro no WeatherService.getLiveWeather:", e);
      return null;
    }
  }
};

const SatelliteService = {
  // Obter rota da textura dinâmica ou padrão do talhão
  getTalhaoTexture: async (farmId) => {
    try {
      const res = await fetch(`${API_URL}/talhao/${farmId}/texture`);
      if (!res.ok) throw new Error("Erro ao obter textura do talhão.");
      return await res.json();
    } catch (e) {
      console.warn("Erro no SatelliteService.getTalhaoTexture:", e);
      return null;
    }
  }
};

/**
 * SERVIÇO DE SIMULAÇÃO WHAT-IF (BACK-END)
 */
const SimulationService = {
  calculateWhatIf: async (nitrogenKg, waterMm, pestPressurePct, areaHa = 42.54) => {
    try {
      const res = await fetch(`${API_URL}/simulation/what-if`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          nitrogen_kg: parseFloat(nitrogenKg),
          water_mm: parseFloat(waterMm),
          pest_pressure_pct: parseFloat(pestPressurePct),
          area_ha: parseFloat(areaHa)
        })
      });
      if (!res.ok) throw new Error("Erro ao processar simulação.");
      return await res.json();
    } catch (e) {
      console.error("Erro no SimulationService.calculateWhatIf:", e);
      return null;
    }
  }
};