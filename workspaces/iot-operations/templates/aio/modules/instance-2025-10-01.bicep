// instance-2025-10-01.bicep
// -------------------------------------------------------------------------------------
// IoT Operations instance and resources at API version 2025-10-01.
// Used by AIO releases: 2512, 2602
//
// Self-contained module: owns all version-specific logic including extension
// configuration, broker defaults, trust derivation, and resource declarations.
// The parent instance.bicep is a thin router that passes raw inputs.
// -------------------------------------------------------------------------------------

import * as types from './types.bicep'
import { aioExtensionName as deriveAioExtensionName, aioExtensionType } from '../../common/extension-names.bicep'

// =====================================================================================
// Parameters (raw inputs from parent)
// =====================================================================================

// Cluster
param clusterName string
param clusterNamespace string
param clusterLocation string

// Instance
param instanceName string
param instanceIdentity object
param schemaRegistryId string
param adrNamespaceId string?
param features types.Features?
param defaultDataflowInstanceCount int

// AIO extension
param aioVersion string
param aioTrain string
param observabilityEnabled bool
param otelCollectorAddress string
param aioConfigurationOverrides object

// Broker (raw, nullable fields)
param brokerConfig types.BrokerConfig?

// TLS
param trustConfig types.TrustConfig

// Cluster reference from parent
param clusterResourceId string

// Custom location
param customLocationName string
param clExtensionIds string[]

// =====================================================================================
// Variables (version-specific logic owned by this module)
// =====================================================================================

var AIO_EXTENSION_NAME = deriveAioExtensionName(clusterResourceId)

var customerManagedTrust = trustConfig.source == 'CustomerManaged'
var ISSUER_NAME = customerManagedTrust
  ? trustConfig.settings.issuerName
  : '${clusterNamespace}-aio-certificate-issuer'
var TRUST_CONFIG_MAP = customerManagedTrust
  ? trustConfig.settings.configMapName
  : '${clusterNamespace}-aio-ca-trust-bundle'

var MQTT_SETTINGS = {
  brokerListenerServiceName: 'aio-broker'
  brokerListenerPort: 18883
  brokerListenerHost: 'aio-broker.${clusterNamespace}'
  serviceAccountAudience: 'aio-internal'
}

var BROKER_CONFIG = {
  frontendReplicas: brokerConfig.?frontendReplicas ?? 2
  frontendWorkers: brokerConfig.?frontendWorkers ?? 2
  backendRedundancyFactor: brokerConfig.?backendRedundancyFactor ?? 2
  backendWorkers: brokerConfig.?backendWorkers ?? 2
  backendPartitions: brokerConfig.?backendPartitions ?? 2
  memoryProfile: brokerConfig.?memoryProfile ?? 'Medium'
  serviceType: brokerConfig.?serviceType ?? 'ClusterIp'
  persistence: brokerConfig.?persistence
  logsLevel: brokerConfig.?logsLevel ?? 'info'
}

var defaultAioConfigurationSettings = {
  AgentOperationTimeoutInMinutes: '120'
  'connectors.values.mqttBroker.address': 'mqtts://${MQTT_SETTINGS.brokerListenerHost}:${MQTT_SETTINGS.brokerListenerPort}'
  'connectors.values.mqttBroker.serviceAccountTokenAudience': MQTT_SETTINGS.serviceAccountAudience
  'dataFlows.values.tinyKube.mqttBroker.hostName': MQTT_SETTINGS.brokerListenerHost
  'dataFlows.values.tinyKube.mqttBroker.port': string(MQTT_SETTINGS.brokerListenerPort)
  'dataFlows.values.tinyKube.mqttBroker.authentication.serviceAccountTokenAudience': MQTT_SETTINGS.serviceAccountAudience
  'observability.metrics.enabled': string(observabilityEnabled)
  'observability.metrics.openTelemetryCollectorAddress': observabilityEnabled ? otelCollectorAddress : ''
  trustSource: trustConfig.source
  'trustBundleSettings.issuer.name': ISSUER_NAME
  'trustBundleSettings.issuer.kind': trustConfig.?settings.?issuerKind ?? ''
  'trustBundleSettings.configMap.name': trustConfig.?settings.?configMapName ?? ''
  'trustBundleSettings.configMap.key': trustConfig.?settings.?configMapKey ?? ''
  'schemaRegistry.values.mqttBroker.host': 'mqtts://${MQTT_SETTINGS.brokerListenerHost}:${MQTT_SETTINGS.brokerListenerPort}'
  'schemaRegistry.values.mqttBroker.serviceAccountTokenAudience': MQTT_SETTINGS.serviceAccountAudience
}

// =====================================================================================
// Existing Resources
// =====================================================================================

resource cluster 'Microsoft.Kubernetes/connectedClusters@2024-07-15-preview' existing = {
  name: clusterName
}

// =====================================================================================
// AIO Extension
// =====================================================================================

resource aioExtension 'Microsoft.KubernetesConfiguration/extensions@2023-05-01' = {
  scope: cluster
  name: AIO_EXTENSION_NAME
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    extensionType: aioExtensionType
    version: aioVersion
    releaseTrain: aioTrain
    autoUpgradeMinorVersion: false
    scope: {
      cluster: {
        releaseNamespace: clusterNamespace
      }
    }
    configurationSettings: union(defaultAioConfigurationSettings, aioConfigurationOverrides)
  }
}

// =====================================================================================
// Custom Location
// =====================================================================================

resource customLocation 'Microsoft.ExtendedLocation/customLocations@2021-08-31-preview' = {
  name: customLocationName
  location: clusterLocation
  properties: {
    hostResourceId: cluster.id
    namespace: clusterNamespace
    displayName: customLocationName
    clusterExtensionIds: [...clExtensionIds, aioExtension.id]
  }
}

var extendedLocation = {
  name: customLocation.id
  type: 'CustomLocation'
}

// =====================================================================================
// AIO Instance
// =====================================================================================

resource aioInstance 'Microsoft.IoTOperations/instances@2025-10-01' = {
  name: instanceName
  location: clusterLocation
  extendedLocation: extendedLocation
  identity: instanceIdentity
  properties: {
    description: 'An AIO instance.'
    schemaRegistryRef: {
      resourceId: schemaRegistryId
    }
    features: features
    adrNamespaceRef: !empty(adrNamespaceId)
      ? {
          resourceId: adrNamespaceId!
        }
      : null
  }
}

// =====================================================================================
// Broker Resources
// =====================================================================================

resource broker 'Microsoft.IoTOperations/instances/brokers@2025-10-01' = {
  parent: aioInstance
  name: 'default'
  extendedLocation: extendedLocation
  properties: {
    memoryProfile: BROKER_CONFIG.memoryProfile
    generateResourceLimits: {
      cpu: 'Disabled'
    }
    cardinality: {
      backendChain: {
        partitions: BROKER_CONFIG.backendPartitions
        workers: BROKER_CONFIG.backendWorkers
        redundancyFactor: BROKER_CONFIG.backendRedundancyFactor
      }
      frontend: {
        replicas: BROKER_CONFIG.frontendReplicas
        workers: BROKER_CONFIG.frontendWorkers
      }
    }
    persistence: BROKER_CONFIG.?persistence
    diagnostics: {
      logs: {
        level: BROKER_CONFIG.logsLevel
      }
    }
  }
}

resource brokerAuthn 'Microsoft.IoTOperations/instances/brokers/authentications@2025-10-01' = {
  parent: broker
  name: 'default'
  extendedLocation: extendedLocation
  properties: {
    authenticationMethods: [
      {
        method: 'ServiceAccountToken'
        serviceAccountTokenSettings: {
          audiences: [
            MQTT_SETTINGS.serviceAccountAudience
          ]
        }
      }
    ]
  }
}

resource brokerListener 'Microsoft.IoTOperations/instances/brokers/listeners@2025-10-01' = {
  parent: broker
  name: 'default'
  extendedLocation: extendedLocation
  properties: {
    serviceType: BROKER_CONFIG.serviceType
    serviceName: MQTT_SETTINGS.brokerListenerServiceName
    ports: [
      {
        authenticationRef: brokerAuthn.name
        port: MQTT_SETTINGS.brokerListenerPort
        tls: {
          mode: 'Automatic'
          certManagerCertificateSpec: {
            issuerRef: {
              name: ISSUER_NAME
              kind: customerManagedTrust ? trustConfig.settings.issuerKind : 'ClusterIssuer'
              group: 'cert-manager.io'
            }
          }
        }
      }
    ]
  }
}

// =====================================================================================
// Dataflow Resources
// =====================================================================================

resource dataflowProfile 'Microsoft.IoTOperations/instances/dataflowProfiles@2025-10-01' = {
  parent: aioInstance
  name: 'default'
  extendedLocation: extendedLocation
  properties: {
    instanceCount: defaultDataflowInstanceCount
  }
}

resource dataflowEndpoint 'Microsoft.IoTOperations/instances/dataflowEndpoints@2025-10-01' = {
  parent: aioInstance
  name: 'default'
  extendedLocation: extendedLocation
  properties: {
    endpointType: 'Mqtt'
    mqttSettings: {
      host: '${MQTT_SETTINGS.brokerListenerHost}:${MQTT_SETTINGS.brokerListenerPort}'
      authentication: {
        method: 'ServiceAccountToken'
        serviceAccountTokenSettings: {
          audience: MQTT_SETTINGS.serviceAccountAudience
        }
      }
      tls: {
        mode: 'Enabled'
        trustedCaCertificateConfigMapRef: TRUST_CONFIG_MAP
      }
    }
  }
}

resource artifactRegistryEndpoint 'Microsoft.IoTOperations/instances/registryEndpoints@2025-10-01' = {
  parent: aioInstance
  name: 'default'
  extendedLocation: extendedLocation
  properties: {
    host: 'mcr.microsoft.com'
    authentication: {
      method: 'Anonymous'
      anonymousSettings: {}
    }
  }
}

// =====================================================================================
// Outputs
// =====================================================================================

output instanceName string = aioInstance.name
output brokerName string = broker.name
output brokerListenerName string = brokerListener.name
output brokerAuthnName string = brokerAuthn.name
output brokerSettings object = { ...BROKER_CONFIG, ...MQTT_SETTINGS }
output aioExtensionName string = aioExtension.name
output aioExtensionId string = aioExtension.id
output aioExtensionVersion string = aioExtension.properties.version
output aioExtensionReleaseTrain string = aioExtension.properties.releaseTrain
output aioExtensionPrincipalId string = aioExtension.identity.principalId
output customLocationId string = customLocation.id
output customLocationName string = customLocation.name
