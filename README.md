# DEXTER 0.1 / Local Jarvis

Minimalny lokalny backend agenta: FastAPI, Ollama HTTP i jawnie dozwolone
narzędzia domu. DEV działa na Silver Monkey. Brak integracji z prawdziwym HA,
pamięci rozmów, voice, schedulerów i cloud fallback.

## Uruchomienie DEV

Wymagania: Docker z Compose v2, Make i Ollama na hoście z pobranym modelem
obsługującym tools (np. `ollama pull qwen3:14b`).

```bash
cp .env.example .env
make up
curl http://localhost:8000/health
curl -X POST http://localhost:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"Jaka jest temperatura na strychu?"}'
```

Ollama musi nasłuchiwać na adresie osiągalnym z sieci Docker. Samo
`127.0.0.1:11434` na hoście nie wystarczy. Skonfiguruj usługę Ollama, np.
`OLLAMA_HOST=0.0.0.0:11434`, z dostępem ograniczonym firewallem do zaufanej
sieci/mostu Docker, i uruchom ją ponownie. Compose mapuje
`host.docker.internal` na bramę hosta również na Linuksie.

`make logs` pokazuje logi, `make shell` otwiera powłokę, `make down` zatrzymuje
DEV. Kod `src/` oraz testy są bind mountami; Uvicorn działa z `--reload`.
Zmiany kodu nie wymagają rebuildowania. Zmiany zależności wymagają `make up`.
Makefile domyślnie wyłącza delegowanie budowy do Bake (`COMPOSE_BAKE=false`),
aby działać także z lokalną konfiguracją pluginów Docker na Silver Monkey.
Port API jest domyślnie udostępniony lokalnie na `127.0.0.1:8000`.
DEV na Silver Monkey zachowuje `DEXTER_PORT=8000`.

## Konfiguracja

| ENV | Znaczenie |
| --- | --- |
| `DEXTER_PORT` | Port hosta w Compose, domyślnie `8000`; port aplikacji w kontenerze zawsze `8000` |
| `APP_ENV` | Etykieta środowiska, domyślnie `dev` |
| `OLLAMA_URL` | Wymagany URL; przykład `http://host.docker.internal:11434` |
| `OLLAMA_MODEL` | Wymagana nazwa modelu, np. `qwen3:14b` |
| `AGENT_MAX_STEPS` | Liczba wywołań LLM oraz limit wykonań tools, 1–5, domyślnie 5 |
| `OLLAMA_TIMEOUT_SECONDS` | Timeout HTTP Ollamy, domyślnie 120 s |

`.env` jest ignorowany przez Git i nie trafia do obrazu. Model i URL nie mają
wartości zaszytych w adapterze. `/health` sprawdza działanie API, nie modelu.
Każde żądanie dostaje UUID w `X-Trace-ID`; `/api/chat` zwraca go też w JSON.
Błędy walidacji requestu zwracają 422. Niedostępność modelu i limit kroków
zwracają odpowiedź agenta z zachowaniem wykonanych `actions` (HTTP 200).

## Testy

```bash
make test
```

Testy używają `MockLLMProvider` i `httpx.MockTransport`, bez działającej Ollamy.
Alternatywnie lokalnie, Python 3.12+:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest -q
```

## Struktura i zasady

```text
src/
  domain/          # kontrakty LLMProvider, Tool, wiadomości
  application/     # AgentEngine, ToolRegistry i ślad akcji
  infrastructure/  # FastAPI, ENV, Ollama i FakeHomeState
tests/             # mock modelu i testy bez usług zewnętrznych
```

`LLMProvider.chat()` obsługuje odpowiedzi tekstowe oraz tool calls. Adapter
Ollama korzysta z [POST /api/chat](https://docs.ollama.com/api/chat), bez streamingu.
Rejestr udostępnia modelowi schematy Pydantic; odrzuca nieznane nazwy,
nieprawidłowe argumenty, dodatkowe pola i nieobsługiwane pokoje.

MVP zna tylko `attic` (strych): temperatura 21.7°C, światło początkowo `off`.
Tools: `get_temperature`, `get_light_state`, `turn_on_light`, `turn_off_light`.
Stan jest wspólny dla żądań w jednym procesie i resetuje się po restarcie
lub reloadzie. Uruchamiaj jeden worker dla tego adaptera in-memory.

Każda próba wykonania toola trafia do `actions` i logu z `trace_id`.
Błędne wywołanie wraca do modelu jako błąd. Maksymalnie 5 wywołań modelu i 5
wykonań narzędzi na request; limit obejmuje także batche. Akcje z ostatniego
kroku mogą zostać wykonane bez kolejnego podsumowania LLM — wtedy odpowiedź
informuje o limicie, a `actions` zachowuje wyniki. Nie ma automatycznych
retry ani rollbacku wykonanych operacji.

## Późniejszy adapter Home Assistant

Dodaj w `infrastructure/` implementacje kontraktu `Tool` i zarejestruj je
zamiast `create_registry(FakeHomeState())` w fabryce API. Pętla agenta i API
pozostają takie same. HA będzie źródłem prawdy o domu; adapter ma odczytywać
stan z HA. Model ma otrzymywać wyłącznie jawnie wystawione narzędzia,
bez tokenu ani dostępu administracyjnego. Walidacja argumentów i trace
pozostają w warstwie aplikacji.

Kod i pliki Compose nie zawierają ścieżek Silver Monkey. Repo może później
trafić do `/srv/dexter/core`; bazowy `compose.yaml` uruchamia kod z obrazu
bez reloadu. Produkcyjny deployment pozostaje poza zakresem tego etapu.


## Port hosta na serwerze DEXTER

Na serwerze port 8000 zajmuje WordTracker NLP. Ustaw w `.env` np.
`DEXTER_PORT=8001`, wybierając wolny port hosta. Compose używa mapowania
`127.0.0.1:${DEXTER_PORT:-8000}:8000`; brak lub pusta wartość oznacza port 8000.
Zmienna jest odczytywana przez Compose z `.env` lub środowiska powłoki.

Przykład uruchomienia bazowej konfiguracji z innym portem:

```bash
DEXTER_PORT=8001 docker compose -f compose.yaml up -d --build
curl http://localhost:8001/health
```

Wewnętrzny port Uvicorna i healthcheck kontenera pozostają na 8000.
