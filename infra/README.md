# infra/ — agent_search_iq の補助リソース(Bicep)

`azure.yaml` は `infra: provider: microsoft.foundry` で、Hosted Agent 自体は既存の
Foundryプロジェクト(`AI_FOUNDRY_PROJECT_ENDPOINT` が指す先)が存在する前提でデプロイされる(`azd provision` の対象外)。

このディレクトリは、その前提となる **土台のAzureリソース** を用意するための独立したBicepテンプレート。
`azd provision` ではなく `az deployment group create` から実行する。

## 作るもの / 作らないもの

作る(ARMで管理できるリソース):
- Azure AI Search サービス(既定 `free` tier、セマンティックランカー無料枠付き)
- Azure AI Foundry アカウント(`Microsoft.CognitiveServices/accounts`, kind=AIServices)+ プロジェクト
- チャットモデル・Embeddingモデルのデプロイ(既定 gpt-4.1-mini / text-embedding-3-small、各1,000TPM)
- (任意, 既定オフ) Microsoft Fabric 容量(F2など、有償)

作らない(ARM/Bicepで管理できない・別途手動が必要):
- Entra IDアプリ登録(OBO用・Fabric IQ接続用)、委任権限の管理者同意
- Fabricワークスペース・セマンティックモデル・オントロジー(Fabricポータル/REST APIの領域)
- Foundryポータル上のFabric IQ Connection、Toolbox(`setup_toolbox.py`)
- 各アカウントでのOAuth同意

これらの手順は `../docs/azure-resources-report.html` のセットアップ順序を参照。

## 使い方

```bash
az group create -n rg-agent-search-iq -l japaneast

az deployment group create \
  -g rg-agent-search-iq \
  -f infra/main.bicep \
  -p infra/main.parameters.json
```

デプロイ後、出力値を `azd env set` などで `.env` に反映する:

```bash
az deployment group show -g rg-agent-search-iq -n main --query properties.outputs
```

`AZURE_SEARCH_API_KEY` / `AZURE_OPENAI_API_KEY` はシークレットのため出力に含めていない。別途取得する:

```bash
az search admin-key show --service-name <searchServiceName> -g rg-agent-search-iq --query primaryKey -o tsv
az cognitiveservices account keys list --name <foundryAccountName> -g rg-agent-search-iq --query key1 -o tsv
```

## 個人利用・最小コストの既定値

- `searchSku=free` — Azure AI Search を無料tierで作成($0、SLA無し・50MB・3インデックス上限)。
  安定運用が必要になったら `basic` に変更(~$73.73/月)。
- `deployFabricCapacity=false` — Fabric容量は作らない。検証はまず
  [Fabricポータルの60日間無料トライアル容量](https://learn.microsoft.com/en-us/fabric/fundamentals/fabric-trial)
  (ARM管理外、`app.fabric.microsoft.com` から開始)を使う方が安い。
  トライアル終了後に有償化する場合だけ `deployFabricCapacity=true` にして再デプロイする。
- チャット/Embeddingモデルのデプロイキャパシティは既定 1(=1,000TPM)の最小構成。スロットリングされる場合のみ増やす。

## モデルバージョンについて

`chatModelVersion` / `embeddingModelVersion` は既定で空文字列にしてあり、その場合Azureが
現在のデフォルトバージョンを自動選択する(モデルのバージョンは提供終了・入れ替わりが頻繁なため、
このテンプレートに特定バージョンを固定で書いていない)。特定バージョンに固定したい場合は、
デプロイ先アカウント作成後に

```bash
az cognitiveservices account list-models --name <foundryAccountName> -g rg-agent-search-iq
```

で利用可能なバージョンを確認してから `main.parameters.json` に指定すること。
