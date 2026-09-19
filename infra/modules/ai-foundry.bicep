@description('Cognitive Services (AI Foundry) account name. Also used as the customSubDomainName, so must be globally unique.')
param accountName string

@description('AI Foundry project name (used in the project endpoint URL)')
param projectName string

param location string
param tags object = {}

param chatModelName string = 'gpt-4.1-mini'
@description('省略するとAzureが現在のデフォルトバージョンを割り当てる。特定版を固定したい場合のみ指定(事前に az cognitiveservices account list-models で利用可能版を確認すること)')
param chatModelVersion string = ''
@allowed(['Standard', 'GlobalStandard'])
param chatModelSku string = 'Standard'
@description('1 = 1,000 TPM。個人検証用の最小値')
param chatModelCapacity int = 1

param embeddingModelName string = 'text-embedding-3-small'
param embeddingModelVersion string = ''
@allowed(['Standard', 'GlobalStandard'])
param embeddingModelSku string = 'Standard'
param embeddingModelCapacity int = 1

var chatModel = union(
  { format: 'OpenAI', name: chatModelName },
  empty(chatModelVersion) ? {} : { version: chatModelVersion }
)
var embeddingModel = union(
  { format: 'OpenAI', name: embeddingModelName },
  empty(embeddingModelVersion) ? {} : { version: embeddingModelVersion }
)

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: accountName
    disableLocalAuth: false
    publicNetworkAccess: 'Enabled'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  properties: {
    displayName: projectName
    description: 'agent_search_iq - Fabric IQ hosted agent project'
  }
}

resource chatDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: account
  name: chatModelName
  sku: {
    name: chatModelSku
    capacity: chatModelCapacity
  }
  properties: {
    model: chatModel
    versionUpgradeOption: 'OnceCurrentVersionExpired'
  }
}

resource embeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: account
  name: embeddingModelName
  sku: {
    name: embeddingModelSku
    capacity: embeddingModelCapacity
  }
  properties: {
    model: embeddingModel
    versionUpgradeOption: 'OnceCurrentVersionExpired'
  }
  // 同一アカウントへの同時デプロイ作成はコンフリクトすることがあるため直列化する
  dependsOn: [
    chatDeployment
  ]
}

output accountName string = account.name
output accountEndpoint string = account.properties.endpoint
output projectName string = project.name
output projectEndpoint string = '${account.properties.endpoint}api/projects/${project.name}'
output principalId string = account.identity.principalId
