# DEXTER — kolekcja Bruno

W Bruno wybierz **Open Collection** i wskaż katalog `bruno/` z tego repo.
Uruchom backend przez `make up` w katalogu głównym projektu, następnie wybierz
środowisko **Silver Monkey DEV**. Wyślij `01 Health / Health`, a potem dowolny
request z `02 Chat`. Wiadomość edytujesz bezpośrednio w JSON w zakładce Body.

## Zmienne lokalne

Jedyną wymaganą zmienną klienta jest `baseUrl`, bez końcowego `/`:

| Środowisko | baseUrl |
| --- | --- |
| Silver Monkey DEV | `http://localhost:8000` |
| DEXTER Server Local | `http://localhost:8001` |

Kolekcja ma też domyślny `baseUrl=http://localhost:8000`, więc działa bez wyboru
środowiska. Wartość środowiska nadpisuje domyślną. Dla innego `DEXTER_PORT`
zmień port w `baseUrl`. Bruno nie odczytuje automatycznie `.env` backendu.
Środowisko serwera zakłada uruchomienie klienta na serwerze lub tunel SSH,
ponieważ Compose publikuje API na interfejsie loopback.

API nie wymaga tokenów ani innych sekretów. `OLLAMA_URL`, `OLLAMA_MODEL`,
`AGENT_MAX_STEPS` i timeout modelu są konfiguracją backendu, nie requestów.
Przy wolnym modelu ustaw timeout requestów w Bruno na co najmniej 660000 ms
(5 kroków po 120 s plus zapas).

## Zawartość

- Health: `GET /health`.
- Chat: własna wiadomość oraz temperatura, stan światła, włączenie i wyłączenie
  światła przez `POST /api/chat`. Wszystkie przykłady mają gotowe treści `message`.
- Walidacja: pusta wiadomość, brak pola, błędny typ i dodatkowe pole — HTTP 422.
- Dokumentacja: OpenAPI JSON, Swagger UI i ReDoc.

Narzędzia nie mają osobnych endpointów HTTP: wywołuje je model przez chat.
Przykłady operują na `attic` (strych). Temperatura fake to 21.7°C; stan światła
jest wspólny dla requestów i resetuje się po restarcie/reloadzie backendu.
Uruchomienie całej kolekcji włącza, a następnie wyłącza fake światło.

Testy weryfikują statusy HTTP, UUID w `X-Trace-ID`, kontrakt `ChatResult`,
strukturę błędów 422 oraz oczekiwane wykonania narzędzi. Nie porównują dokładnej
treści odpowiedzi modelu. Przykłady narzędzi wymagają działającego providera LLM:
brak oczekiwanej akcji powoduje błąd testu, nawet gdy API zwróci HTTP 200.
Test własnej wiadomości sprawdza sam kontrakt i może przejść również wtedy,
gdy backend zgłasza niedostępność modelu.

## CLI

Po zainstalowaniu [Bruno CLI](https://docs.usebruno.com/bru-cli/overview):

```bash
cd bruno
bru run --env 'Silver Monkey DEV'
# Kontrola niezależna od Ollamy:
bru run '01 Health' --env 'Silver Monkey DEV'
bru run '03 Walidacja' --env 'Silver Monkey DEV'
bru run '04 Dokumentacja' --env 'Silver Monkey DEV'
```

Zgodnie z `AGENTS.md` kolekcja jest częścią kontraktu API. Każda zmiana
endpointów, requestów lub odpowiedzi wymaga aktualizacji plików `.bru`,
przykładów, asercji i, jeśli potrzeba, zmiennych środowiska.


## Wybór transportu LLM

Kolekcja działa z `LLM_PROVIDER=ollama` i `LLM_PROVIDER=rabbitmq` bez zmiany
requestów ani asercji. Provider jest wybierany w ENV backendu, nie w Bruno.
Dla RabbitMQ pełne wykonanie przykładów tools wymaga workera obsługującego
AMQP `reply_to` oraz `RABBITMQ_REPLY_TO_ENABLED=true`. Obecny opisany worker
wysyła wyniki tylko do `llm.results`, więc round-trip PROD jest zablokowany.
Szczegóły wymaganej zmiany są w głównym README. Health i walidacja nadal działają
bez brokera i modelu. Dla RabbitMQ ustaw timeout klienta na co najmniej 1560000 ms
(5 kroków po 300 s plus czas zamknięcia połączeń i zapas).
