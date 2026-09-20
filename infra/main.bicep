// agent_search_iq の補助リソース(AI Search / AI Foundryアカウント+プロジェクト+モデルデプロイ / Fabric容量)。
//
// azure.yaml は `infra: provider: microsoft.foundry` を使っており、azd はホスト先の
// Foundryプロジェクト(AI_FOUNDRY_PROJECT_ENDPOINT が指す先)が既に存在する前提で Hosted Agent 自体をデプロイする
// (azd provision の対象ではない)。このBicepはその前提となる土台のリソースを用意するためのもので、
// `az deployment group create` から単独で実行する。azd の provider は変更しない。
//
// Entra IDアプリ登録・Fabricワークスペース/セマンティックモデル/オントロジー・Fabric IQ接続・
// OAuth同意は ARM では管理できないため対象外(README / docs/azure-resources-report.html 参照)。
//
// 使い方:
//   az group create -n rg-agent-search-iq -l japaneast
//   az deployment group create -g rg-agent-search-iq -f infra/main.bicep -p infra/main.parameters.json

targetScope = 'resourceGroup'

@description('環境名。リソース名の一部に使う')
@minLength(3)
@maxLength(20)
param environmentName string = 'agentsearchiq'

param location string = resourceGroup().location

@description('Azure AI Searchのsku。free=$0(SLA無し・50MB・3インデックス上限)、basicは安定運用向け(~$73.73/月)')
@allowed([
  'free'
  'basic'
  'standard'
])
param searchSku string = 'free'

@description('AI Searchのインデックス名。元テナントへ反映する際は、既存エージェントとインデックスを共有しないよう別名にすること')
param searchIndexName string = 'poc-documents'

param chatModelName string = 'gpt-4.1-mini'
param chatModelVersion string = ''
param chatModelCapacity int = 1

param embeddingModelName string = 'text-embedding-3-small'
param embeddingModelVersion string = ''
param embeddingModelCapacity int = 1

@description('trueにするとMicrosoft.Fabric/capacitiesで有償のF-SKU容量を作成する。既定はfalse。Direct Lake/オントロジーの検証はまずFabricポータルの60日間無料トライアル容量(ARM管理外)を使うほうが安い')
param deployFabricCapacity bool = false

@allowed(['F2', 'F4', 'F8', 'F16', 'F32', 'F64'])
param fabricSkuName string = 'F2'

@description('deployFabricCapacity=trueのとき必須。Fabric容量管理者のEntra ID UPNまたはオブジェクトID')
param fabricAdminMembers array = []

var resourceToken = uniqueString(resourceGroup().id, environmentName)
var namePrefix = toLower(environmentName)
var tags = {
  'azd-env-name': environmentName
  project: 'agent-search-iq'
}

module search 'modules/search.bicep' = {
  name: 'search'
  params: {
    name: 'srch-${namePrefix}-${resourceToken}'
    location: location
    tags: tags
    sku: searchSku
  }
}

module foundry 'modules/ai-foundry.bicep' = {
  name: 'foundry'
  params: {
    accountName: 'aif-${namePrefix}-${resourceToken}'
    projectName: namePrefix
    location: location
    tags: tags
    chatModelName: chatModelName
    chatModelVersion: chatModelVersion
    chatModelCapacity: chatModelCapacity
    embeddingModelName: embeddingModelName
    embeddingModelVersion: embeddingModelVersion
    embeddingModelCapacity: embeddingModelCapacity
  }
}

// Fabric容量名は小文字英数字のみ(ハイフン不可)
module fabric 'modules/fabric-capacity.bicep' = if (deployFabricCapacity) {
  name: 'fabric'
  params: {
    name: 'fab${replace(namePrefix, '-', '')}${resourceToken}'
    location: location
    tags: tags
    skuName: fabricSkuName
    adminMembers: fabricAdminMembers
  }
}

// --- azd env set / .env に流し込む値 ---
// AZURE_SEARCH_API_KEY / AZURE_OPENAI_API_KEY はシークレットのためoutputに含めない。
// デプロイ後に別途取得すること:
//   az search admin-key show --service-name <AZURE_SEARCH_ENDPOINTのホスト名先頭> -g <rg> --query primaryKey -o tsv
//   az cognitiveservices account keys list --name <accountName> -g <rg> --query key1 -o tsv
output AZURE_SEARCH_ENDPOINT string = search.outputs.endpoint
output AZURE_SEARCH_INDEX_NAME string = searchIndexName

output AZURE_OPENAI_ENDPOINT string = foundry.outputs.accountEndpoint
output AZURE_OPENAI_EMBEDDING_DEPLOYMENT string = embeddingModelName
output AZURE_OPENAI_API_VERSION string = '2024-10-21'

output AI_FOUNDRY_PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
output AI_FOUNDRY_MODEL string = chatModelName
output AZURE_AI_MODEL_DEPLOYMENT_NAME string = chatModelName

output foundryAccountName string = foundry.outputs.accountName
output searchServiceName string = search.outputs.name
output fabricCapacityId string = fabric.?outputs.?id ?? ''
