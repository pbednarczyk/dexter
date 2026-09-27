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
| `LLM_PROVIDER` | `ollama` (domyślnie) albo `rabbitmq` |
| `LLM_OPTIONS` | JSON opcji modelu, domyślnie `{}`, przekazywany przez oba adaptery |
| `OLLAMA_URL` | URL wymagany tylko dla `ollama`; przykład `http://host.docker.internal:11434` |
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

Testy używają `MockLLMProvider`, `httpx.MockTransport` i mockowanego transportu
AMQP, bez działającej Ollamy ani RabbitMQ.
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

## Obsługa API w Bruno

Otwórz katalog [`bruno/`](bruno/) jako kolekcję i wybierz **Silver Monkey DEV**
(`baseUrl=http://localhost:8000`). Kolekcja zawiera healthcheck, gotowe wiadomości
chat dla wszystkich czterech narzędzi domu, własną wiadomość, przykłady błędów
walidacji oraz endpointy dokumentacji. Dla serwera wybierz **DEXTER Server Local**
i dostosuj port w `baseUrl` do `DEXTER_PORT`.

Instrukcje, zmienne i uruchamianie asercji opisano w [bruno/README.md](bruno/README.md).
Kolekcja stanowi część kontraktu API i musi być aktualizowana razem z endpointami.


## Provider RabbitMQ — przygotowanie DEXTER 0.2

`AgentEngine → LLMProvider` pozostaje bez zmian. Domyślny adapter Ollama wysyła
HTTP bezpośrednio do modelu. Opcjonalny `RabbitMQLLMProvider` publikuje `llm.chat`
na istniejące `llm.jobs` w vhoście `/wordtracker` i czeka na wynik na prywatnej
kolejce. Każde wywołanie ma własne połączenie, UUID i kolejkę exclusive,
auto-delete z nazwą nadaną przez brokera. DEXTER nigdy nie deklaruje ani nie
konsumuje `llm.results` i nie zmienia topologii współdzielonych kolejek.

**Integracja PROD jest zablokowana do czasu zmiany workera.** Według dostarczonego
kontraktu obecny `dexter-worker` publikuje sztywno na `llm.results`.
Nie modyfikowano ani nie weryfikowano kodu tego osobnego repozytorium.
DEXTER domyślnie odmawia publikacji przez RabbitMQ; samo ustawienie
`LLM_PROVIDER=rabbitmq` nie uruchamia jobów. Testy z mockiem sprawdzają stronę
DEXTER-a dla przyszłego workera, a nie gotowy produkcyjny round-trip.

### Minimalna zmiana dexter-worker (osobne zadanie)

Dla obu wyników `completed` i `failed` worker powinien wybrać docelową kolejkę
z właściwości AMQP **odebranego requestu**:

```python
result_routing_key = request_properties.reply_to or "llm.results"
# existing result publish:
# exchange="", routing_key=result_routing_key
# correlation_id=job_id, message_id=job_id, type="llm.chat.result"
```

JSON requestu i resultu, `schema_version=1`, vhost i `llm.jobs` pozostają takie
same. Worker publikuje wynik wyłącznie do wybranego miejsca, nie do obu:
WordTracker bez `reply_to` nadal otrzymuje wynik na `llm.results`, a DEXTER na
prywatnej kolejce. Konto workera musi móc publikować przez default exchange do
tej kolejki; konto DEXTER-a musi móc deklarować i konsumować własne kolejki.

Jeśli prywatna kolejka zniknęła po timeoutcie, późnego wyniku nie wolno kierować
awaryjnie do `llm.results` ani bez końca ponawiać joba. Worker powinien rozpoznać
brak odbiorcy i zakończyć obsługę tego joba z logiem. Nie wymaga to zmiany
zachowania klientów bez `reply_to`.

### Konfiguracja na Mac Mini

Po wdrożeniu i sprawdzeniu obsługi `reply_to` we wszystkich workerach obsługujących
`llm.jobs`, ustaw w lokalnym `.env` (hasła nie commituj):

```dotenv
LLM_PROVIDER=rabbitmq
OLLAMA_MODEL=qwen3:14b
LLM_OPTIONS={}
RABBITMQ_HOST=host.docker.internal
RABBITMQ_PORT=5672
RABBITMQ_VHOST=/wordtracker
RABBITMQ_USER=wordtracker
RABBITMQ_PASSWORD=<lokalny-sekret>
RABBITMQ_LLM_JOBS_QUEUE=llm.jobs
RABBITMQ_LLM_TIMEOUT=300
RABBITMQ_REPLY_TO_ENABLED=true
```

`RABBITMQ_HOST` wskazuje broker osiągalny z kontenera — podany przykład zakłada
brokera na hoście Mac Mini. Jeśli broker działa w tej samej sieci Docker, użyj
jego nazwy DNS. `OLLAMA_URL` jest zbędny dla RabbitMQ; model wybiera nadal
`OLLAMA_MODEL`. `RABBITMQ_REPLY_TO_ENABLED` to potwierdzenie operatora, nie
negocjacja możliwości workera. Pozostaw `false`, dopóki worker nie jest gotowy.
DEV zachowuje `LLM_PROVIDER=ollama`, port hosta 8000 i dotychczasowy URL Ollamy.
Compose przekazuje ENV przez istniejący `env_file`; nie dodaje usługi brokera.
Po zmianie zależności przebuduj obraz przez `make up`.

### Semantyka i błędy

Obecny kontrakt projektu to już `async def chat(...)`. Wywołujący nadal czeka na
odpowiedź — nie ma endpointów jobów/pollingu, bazy ani nowej pętli agenta.
Użyto `aio-pika` zamiast blokującego Pika, żeby zachować ten kontrakt i nie
blokować healthchecka podczas oczekiwania na model.

Job zachowuje natywne `messages`, `tools`, `tool_calls` i `options`. Publikacja
używa default exchange, `mandatory=true`, publisher confirms, persistent
message i właściwości `correlation_id`, `message_id`, `type`, `reply_to`.
Provider sprawdza wersję schematu, typ JSON i AMQP, job UUID, correlation ID,
status oraz zawartość `message`/`error`. Niepoprawne odpowiedzi są odrzucane
bez requeue. Szczegóły błędów workera/transportu nie trafiają do odpowiedzi API.

`RABBITMQ_LLM_TIMEOUT` (domyślnie 300 s) ogranicza całość jednego wywołania:
połączenie, deklarację, potwierdzenie publikacji i wynik. Zamknięcie połączenia
ma osobny limit 5 s. Nie ma retry ani drugiego joba po timeoutcie/zerwaniu
połączenia; opublikowany job może nadal wykonywać się na workerze. Zamknięcie
połączenia usuwa prywatną kolejkę. Brak potwierdzenia publikacji nie oznacza,
że broker nie przyjął joba.

`/health` nadal sprawdza wyłącznie proces. Wybór providera nie otwiera połączenia
z brokerem przy starcie aplikacji. `POST /api/chat`, UUID i `actions` zachowują
ten sam kontrakt; kontrolowany `ProviderError` obsługuje istniejący AgentEngine.

Opis mechanizmu: [RabbitMQ RPC](https://www.rabbitmq.com/tutorials/tutorial-six-python)
i [API aio-pika](https://docs.aio-pika.com/apidoc.html).
