@description('Fabric capacity name。小文字英数字のみ、先頭は英字(3-63文字)')
param name string

param location string
param tags object = {}

@allowed(['F2', 'F4', 'F8', 'F16', 'F32', 'F64'])
param skuName string = 'F2'

@description('容量管理者のEntra ID UPNまたはオブジェクトID(1件以上必須)')
param adminMembers array

resource capacity 'Microsoft.Fabric/capacities@2023-11-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: 'Fabric'
  }
  properties: {
    administration: {
      members: adminMembers
    }
  }
}

output id string = capacity.id
output name string = capacity.name
