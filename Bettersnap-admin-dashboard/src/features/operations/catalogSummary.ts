import type { CatalogResponse } from '../../services'

export function catalogSummary(catalog: CatalogResponse) {
  return catalog.categories.reduce(
    (summary, category) => ({
      categories: summary.categories + 1,
      attires: summary.attires + category.attires.length,
      backgrounds: summary.backgrounds + category.backgrounds.length,
    }),
    { categories: 0, attires: 0, backgrounds: 0 },
  )
}
