theorem phase10_l5_extensional (xs ys : List Nat)
    (hxy : ∀ x, x ∈ xs → x ∈ ys) (hyx : ∀ x, x ∈ ys → x ∈ xs) : xs = ys := by
  {{proof}}
