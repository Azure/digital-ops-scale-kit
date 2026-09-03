param customLocationName string
param adrNamespaceName string

resource customLocation 'Microsoft.ExtendedLocation/customLocations@2021-08-31-preview' existing = {
  name: customLocationName
}

resource adrNamespace 'Microsoft.DeviceRegistry/namespaces@2025-10-01' existing = {
  name: adrNamespaceName
}

resource externalDevice 'Microsoft.DeviceRegistry/namespaces/devices@2025-10-01' = {
  name: 'external-opc-ua'
  parent: adrNamespace
  location: resourceGroup().location
  extendedLocation: {
    type: 'CustomLocation'
    name: customLocation.id
  }
  properties: {
    enabled: true
    endpoints: {
      outbound: {
        assigned: {}
      }
      inbound: {
        'opc-ua-connector-0': {
          endpointType: 'Microsoft.OpcUa'
          address: 'opc.tcp://resource-set-opc-plc:50000'
          authentication: {
            method: 'Anonymous'
          }
        }
      }
    }
  }
}
