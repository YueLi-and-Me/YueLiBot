// 对照实际响应字段与 TS 类型；引用、继承、数组和动态字典都由编译器展开。
const ts = require('typescript')
const fs = require('node:fs')
const path = require('node:path')
const file = path.resolve(__dirname, '../webui/src/features/tools/types.ts')
const program = ts.createProgram([file], { strict: true, target: ts.ScriptTarget.ESNext })
const checker = program.getTypeChecker()
const source = program.getSourceFile(file)
const declarations = new Map()
ts.forEachChild(source, node => {
  if (ts.isInterfaceDeclaration(node)) declarations.set(node.name.text, checker.getTypeAtLocation(node))
})
function check(value, type, location) {
  if (type.flags & ts.TypeFlags.Unknown) return
  if (type.isUnion()) {
    if (value === null && type.types.some(t => t.flags & ts.TypeFlags.Null)) return
    const objects = type.types.filter(t => t.flags & ts.TypeFlags.Object)
    if (!objects.length) return
    type = objects[0]
  }
  if (checker.isArrayType(type)) {
    if (!Array.isArray(value)) throw new Error(`${location}: 响应不是数组`)
    for (const entry of value) check(entry, checker.getTypeArguments(type)[0], `${location}[]`)
    return
  }
  if (!(type.flags & ts.TypeFlags.Object)) return
  if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error(`${location}: 响应不是对象`)
  const index = checker.getIndexTypeOfType(type, ts.IndexKind.String)
  if (index) {
    for (const [key, item] of Object.entries(value)) check(item, index, `${location}.${key}`)
    return
  }
  const properties = checker.getPropertiesOfType(type)
  const actual = Object.keys(value).sort()
  const expected = properties.map(p => p.name).sort()
  if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new Error(`${location}: 后端 ${actual}; 前端 ${expected}`)
  for (const property of properties) check(value[property.name], checker.getTypeOfSymbolAtLocation(property, property.valueDeclaration), `${location}.${property.name}`)
}
const responses = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))
for (const [name, response] of Object.entries(responses)) {
  if (!declarations.has(name)) throw new Error(`前端缺少 ${name}`)
  check(response, declarations.get(name), name)
}
console.log(`CONTRACT_OK=${Object.keys(responses).length}`)
