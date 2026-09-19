@description('Azure AI Search service name (lowercase letters, numbers, dashes; 2-60 chars)')
param name string

param location string
param tags object = {}

@description('free = $0/月だがSLA無し・50MB・3インデックスまで。basic = 安定運用できる最小の有償tier(~$73.73/月)')
@allowed([
  'free'
  'basic'
  'standard'
])
param sku string = 'free'

@description('セマンティックランカーのプラン。freeは月1,000クエリまで無料(tierを問わず利用可)')
@allowed([
  'disabled'
  'free'
  'standard'
])
param semanticSearchPlan string = 'free'

resource search 'Microsoft.Search/searchServices@2024-06-01-preview' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: sku
  }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
    semanticSearch: semanticSearchPlan
  }
}

output endpoint string = 'https://${search.name}.search.windows.net'
output name string = search.name
