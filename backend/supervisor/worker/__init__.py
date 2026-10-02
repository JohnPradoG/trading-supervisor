"""Worker de operaciones: convierte los eventos crudos del EA (raw_events) en trades y
trade_events.

Es de solo lectura frente a MT5: lee de la base de datos lo que el EA envió y escribe el
registro derivado. No existe código que envíe órdenes.

Reglas:
- Se procesan los raw_events PENDING por orden de la hora del hecho (event_time), en lotes
  pequeños con SELECT ... FOR UPDATE SKIP LOCKED y cada evento en su propio savepoint.
- Resultado por evento: PROCESSED (se aplicó, o ya estaba aplicado), IGNORED (no aporta nada:
  modificación sin cambios, sobre una operación cerrada o de una posición desconocida pasado
  el plazo de espera), FAILED (error determinista, con el texto en `error`; se reintenta con
  `supervisor-cli reprocess`). La foto de posiciones siempre queda PROCESSED: se reconcilia
  aunque no cambie nada.
- Un cierre o modificación que llega antes que su apertura (backfill) no se pierde: queda
  PENDING con `next_attempt_at` y reintentos espaciados. Cuando llega la apertura, sus
  eventos en espera se reactivan al momento; si pasa el plazo (worker_defer_max_seconds) el
  cierre queda FAILED con el prefijo "huérfano:" y se reactiva igualmente si la apertura
  aparece después.
- trades solo se actualiza (nunca se borra) y trade_events es de solo inserción. Reprocesar es
  idempotente: un deal solo entra una vez por operación (ON CONFLICT DO NOTHING) y los demás
  eventos se comprueban por raw_event_id.
"""
