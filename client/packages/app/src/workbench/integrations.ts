import type { AxiosError } from 'axios'
import Cookies from 'js-cookie'
import instance from '@/service/axiosInstance'

const CSRF_COOKIE = 'claw_workbench_csrf'

export type IntegrationConnection = {
  credential_id?: string
  provider_id?: string
  connector_id?: string
  status: string
  revocation_status?: string
  granted_scopes?: string[]
  token_type?: string
  expires_at?: string | null
  connected_at?: string
  execution_status: string
}

export type IntegrationResource = {
  kind: 'skill' | 'plugin' | 'tool' | 'connector'
  resource_id: string
  parent_resource_id: string | null
  name_zh: string
  name_en: string
  provider_id: string | null
  requires_oauth: boolean
  authorization_mode: 'provider_managed' | 'developer_oauth'
  binding_status: string
  provider_sync_status: string
  connection: IntegrationConnection
  can_bind: boolean
  blocked_reason: string | null
}

export type IntegrationCatalog = {
  contract_version: string
  provider_contract_retrieved_at: string
  resources: IntegrationResource[]
  execution_policy: Record<IntegrationResource['kind'], string>
}

const mutationHeaders = () => {
  const token = Cookies.get(CSRF_COOKIE)
  if (!token) throw new Error('workbench CSRF token is unavailable')
  return { 'X-Workbench-CSRF': token }
}

export const describeIntegrationError = (error: unknown): string => {
  const axiosError = error as AxiosError<{ description?: string; message?: string; error?: { message?: string } }>
  return axiosError.response?.data?.error?.message
    || axiosError.response?.data?.description
    || axiosError.response?.data?.message
    || (error instanceof Error ? error.message : 'integration request failed')
}

export const listIntegrations = async (): Promise<IntegrationCatalog> => (
  await instance.get('/integrations') as unknown as IntegrationCatalog
)

export const changeIntegrationBinding = async (
  resource: IntegrationResource,
  action: 'bind' | 'unbind',
): Promise<void> => {
  await instance.post('/integrations/bindings', {
    action,
    kind: resource.kind,
    resource_id: resource.resource_id,
    parent_resource_id: resource.parent_resource_id,
  }, { headers: mutationHeaders() })
}

export const startConnectorOAuth = async (connectorId: string): Promise<string> => {
  const response = await instance.post(
    `/integrations/connectors/${encodeURIComponent(connectorId)}/oauth/start`,
    {},
    { headers: mutationHeaders() },
  ) as unknown as { authorization_url: string }
  return response.authorization_url
}

export const disconnectConnector = async (connectorId: string): Promise<void> => {
  await instance.post(
    `/integrations/connectors/${encodeURIComponent(connectorId)}/disconnect`,
    {},
    { headers: mutationHeaders() },
  )
}
