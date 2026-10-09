/** 筛选已检索设施；不会把其他设施的名称和入口拼接成虚假的匹配。 */
export function normalizeTripQuery(value: string): string {
  return value.normalize('NFKC').toLocaleLowerCase().replace(/[\s\p{P}\p{S}]/gu, '')
}

export function matchesTripQuery(name: string, gates: readonly (string | null)[], query: string, aliases: readonly string[] = []): boolean {
  const needle = normalizeTripQuery(query)
  if (!needle) return true
  const texts = [name, ...aliases, ...gates.filter((gate): gate is string => Boolean(gate))].map(normalizeTripQuery)
  if (texts.some(text => text.includes(needle))) return true
  // 允许「朝春小学」匹配「上海市朝春中心小学」等中文简称；短词仍须连续匹配。
  if (!/^[\p{Script=Han}]{4,}$/u.test(needle)) return false
  return texts.some(text => {
    let index = 0
    for (const character of text) if (character === needle[index]) index++
    return index === needle.length
  })
}
