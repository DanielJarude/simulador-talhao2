// app.js - Camada de Integração Front-end <-> Back-end FastAPI (Orion Agro)
// As URLs usam base relativa a `API_URL` (única constante de ambiente do front).
//
// PR #4 — resolução da base da API:
//   1. `window.ORION_API_URL` (se definido antes deste script) tem prioridade —
//      permite deploy/preview sem editar arquivos;
//   2. página servida pelo próprio backend (FastAPI, mesma origem) → URL
//      RELATIVA (sem localhost hardcoded; funciona no preview/deploy);
//   3. página servida por servidor estático separado (dev local, :5501) →
//      `http://localhost:8000/api` (comportamento histórico preservado).
const API_URL = (
  window.ORION_API_URL ||
  (["localhost", "127.0.0.1"].indexOf(window.location.hostname) >= 0
    ? "http://localhost:8000/api"
    : "/api")
).replace(/\/+$/, "");

// ---------------------------------------------------------------------------
// Autenticação (JWT)
// ---------------------------------------------------------------------------
const TOKEN_KEY = "orion_auth_token";
const USER_KEY = "orion_auth_user";

/** Headers de autorização a partir do token em localStorage. */
function authHeaders() {
  const token = localStorage.getItem(TOKEN_KEY);
  return token ? { "Authorization": `Bearer ${token}` } : {};
}

function storeSession(data) {
  // `data` pode ser {access_token, user} (login) ou o usuário direto
  if (data && data.access_token) {
    localStorage.setItem(TOKEN_KEY, data.access_token);
    if (data.user) localStorage.setItem(USER_KEY, JSON.stringify(data.user));
    return data.user;
  }
  localStorage.setItem(USER_KEY, JSON.stringify(data));
  return data;
}

/**
 * SERVIÇO DE AUTENTICAÇÃO
 */
const AuthService = {
  // Login de Usuário — devolve e persiste o token JWT
  login: async (email, password) => {
    try {
      const res = await fetch(`${API_URL}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password })
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || "E-mail ou senha incorretos.");
      }
      const data = await res.json();
      const user = storeSession(data);
      return { success: true, user, token: data.access_token || null };
    } catch (e) {
      return { success: false, message: e.message };
    }
  },

  // Cadastro de Novo Usuário — após criar a conta, faz login automático
  // para já obter o token (necessário p/ criar/editar fazendas).
  // Obs.: o papel (role) NÃO é enviado — a API atribui o papel público
  // padrão de forma fixa (auto-registro como admin é impossível).
  register: async (name, email, password, _role = undefined) => {
    try {
      const res = await fetch(`${API_URL}/auth/register`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, email, password })
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || "Erro ao cadastrar usuário.");
      }
      await res.json(); // UserResponse
      return await this.login(email, password);
    } catch (e) {
      return { success: false, message: e.message };
    }
  },

  // Recupera usuário ativo da sessão
  getCurrentUser: () => {
    try {
      return JSON.parse(localStorage.getItem(USER_KEY));
    } catch {
      return null;
    }
  },

  // Token atual (ou null)
  getAuthToken: () => localStorage.getItem(TOKEN_KEY),

  // Encerra a sessão
  logout: () => {
    localStorage.removeItem(USER_KEY);
    localStorage.removeItem(TOKEN_KEY);
  }
};

/**
 * SERVIÇO DE GESTÃO DE PROPRIEDADES & TALHÕES (CRUD COMPLETO)
 * As mutações (create/update/delete) exigem Bearer token.
 */
const FarmService = {
  // Listar fazendas do usuário logado (PR #3 — privacidade: exige Bearer token)
  getFarms: async () => {
    try {
      const res = await fetch(`${API_URL}/farms`, { headers: { ...authHeaders() } });
      if (res.status === 401) {
        AuthService.logout();
        throw new Error("Sessão inválida ou expirada — faça login novamente (auth.html).");
      }
      if (res.status === 403) {
        throw new Error("Você não tem permissão para visualizar essas propriedades.");
      }
      if (!res.ok) throw new Error("Falha ao buscar lista de propriedades.");
      return await res.json();
    } catch (e) {
      console.error("Erro no FarmService.getFarms:", e);
      throw e;
    }
  },

  // Buscar detalhes de uma fazenda por ID (PR #3 — exige Bearer token)
  getFarmById: async (id) => {
    try {
      const res = await fetch(`${API_URL}/farms/${id}`, { headers: { ...authHeaders() } });
      if (res.status === 401) {
        AuthService.logout();
        throw new Error("Sessão inválida ou expirada — faça login novamente (auth.html).");
      }
      if (res.status === 403) {
        throw new Error("Você não tem permissão para acessar esta propriedade.");
      }
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
        headers: { "Content-Type": "application/json", ...authHeaders() },
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

      if (res.status === 401) {
        throw new Error("Sessão expirada — faça login novamente (auth.html).");
      }
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
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
        headers: { "Content-Type": "application/json", ...authHeaders() },
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

      if (res.status === 401) {
        throw new Error("Sessão expirada — faça login novamente (auth.html).");
      }
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
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
        method: "DELETE",
        headers: { ...authHeaders() }
      });

      if (res.status === 401) {
        throw new Error("Sessão expirada — faça login novamente (auth.html).");
      }
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
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
  // (PR #3 — exige Bearer token; routes de fazenda são privadas)
  getLiveWeather: async (farmId) => {
    try {
      const res = await fetch(`${API_URL}/weather/farm/${farmId}`, { headers: { ...authHeaders() } });
      if (res.status === 401) { AuthService.logout(); throw new Error("Sessão expirada — faça login novamente (auth.html)."); }
      if (res.status === 403) throw new Error("Sem permissão para os dados desta fazenda.");
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
  // (PR #3 — exige Bearer token; a textura é privada por fazenda)
  getTalhaoTexture: async (farmId) => {
    try {
      const res = await fetch(`${API_URL}/talhao/${farmId}/texture`, { headers: { ...authHeaders() } });
      if (res.status === 401) { AuthService.logout(); throw new Error("Sessão expirada — faça login novamente (auth.html)."); }
      if (res.status === 403) throw new Error("Sem permissão para a textura desta fazenda.");
      if (!res.ok) throw new Error("Erro ao obter textura do talhão.");
      return await res.json();
    } catch (e) {
      console.warn("Erro no SatelliteService.getTalhaoTexture:", e);
      return null;
    }
  },

  // Obter heightmap topográfico (Copernicus DEM GL-30) do talhão.
  // available=false → o 3D mantém o deslocamento via NDVI (fallback).
  // (PR #3 — exige Bearer token)
  getTalhaoHeightmap: async (farmId) => {
    try {
      const res = await fetch(`${API_URL}/talhao/${farmId}/heightmap`, { headers: { ...authHeaders() } });
      if (res.status === 401) { AuthService.logout(); throw new Error("Sessão expirada — faça login novamente (auth.html)."); }
      if (res.status === 403) throw new Error("Sem permissão para o heightmap desta fazenda.");
      if (!res.ok) throw new Error("Erro ao obter heightmap do talhão.");
      return await res.json();
    } catch (e) {
      console.warn("Erro no SatelliteService.getTalhaoHeightmap:", e);
      return null;
    }
  }
};

/**
 * SERVIÇO DE LAUDOS (PDF) — endpoint privado por fazenda (PR #3)
 * Baixa o laudo via fetch autenticado (Bearer) e dispara o download do blob,
 * já que `window.open` não envia o cabeçalho de autorização.
 */
const ReportService = {
  downloadFarmPdf: async (farmId, { layer = "ndvi", dateIndex = 0, nKg = 0, wMm = 0, pestPct = 0 } = {}) => {
    const url = `${API_URL}/reports/farm/${farmId}/pdf?layer=${layer}&date_index=${dateIndex}&n_kg=${nKg}&w_mm=${wMm}&pest_pct=${pestPct}`;
    try {
      const res = await fetch(url, { headers: { ...authHeaders() } });
      if (res.status === 401) { AuthService.logout(); throw new Error("Sessão expirada — faça login novamente (auth.html)."); }
      if (res.status === 403) throw new Error("Você não tem permissão para o laudo desta fazenda.");
      if (!res.ok) throw new Error("Erro ao gerar o laudo em PDF.");
      const blob = await res.blob();
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = `Laudo_Agronomico_fazenda_${farmId}.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(objectUrl);
      return true;
    } catch (e) {
      console.error("Erro no ReportService.downloadFarmPdf:", e);
      throw e;
    }
  }
};

/**
 * SERVIÇO DE SIMULAÇÃO WHAT-IF (BACK-END, protegida por JWT)
 */
const SimulationService = {
  calculateWhatIf: async (nitrogenKg, waterMm, pestPressurePct, areaHa = 42.54) => {
    try {
      const res = await fetch(`${API_URL}/simulation/what-if`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({
          nitrogen_kg: parseFloat(nitrogenKg),
          water_mm: parseFloat(waterMm),
          pest_pressure_pct: parseFloat(pestPressurePct),
          area_ha: parseFloat(areaHa)
        })
      });
      if (res.status === 401) {
        throw new Error("Sessão expirada — faça login novamente (auth.html).");
      }
      if (!res.ok) throw new Error("Erro ao processar simulação.");
      return await res.json();
    } catch (e) {
      console.error("Erro no SimulationService.calculateWhatIf:", e);
      return null;
    }
  }
};
