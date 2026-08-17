import { v4 as uuid } from 'uuid'

/**
 * Stable idempotency key — same operation + same stable IDs always returns same key.
 * Use for replay-safe operations where the resource already has a known ID.
 */
export const generateIdempotencyKey = (
  operation: string,
  ...stableIds: string[]
): string => {
  const raw = [operation, ...stableIds].join(':')
  return btoa(raw).replace(/[+/=]/g, '').slice(0, 64)
}

/**
 * Unique idempotency key — includes a UUID nonce plus any provided stable IDs.
 * Use for first-time creates where no stable resource ID exists yet, or when
 * you want a unique-but-descriptive key that incorporates known stable values.
 */
export const generateUniqueIdempotencyKey = (
  operation: string,
  ...stableIds: string[]
): string => generateIdempotencyKey(operation, uuid(), ...stableIds)
