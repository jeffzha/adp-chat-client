import type { IntegrationResource } from './integrations'

export const integrationBindingPolicy = (resource: IntegrationResource) => ({
  canMutate: resource.can_bind && !resource.blocked_reason,
  usesLocalOAuth: false as const,
  physicalUnbind: false as const,
})
