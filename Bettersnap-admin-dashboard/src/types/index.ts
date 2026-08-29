export type Status = 'active' | 'inactive' | 'pending' | 'succeeded' | 'failed' | 'warning' | 'healthy'
export interface User { id: string; name: string; email: string; role: string; status: Status; joinedAt: string; credits: number }
export interface Job { id: string; type: string; user: string; status: Status; createdAt: string; duration: string }
export interface ApiResponse<T> { data: T; message?: string }
export interface Paginated<T> { items: T[]; total: number; page: number; pageSize: number }
export interface Health { status: string; face_gate: string }
export interface Plan { key: string; name: string; plan_type: string; image_count: number; max_attires: number; max_backgrounds: number; category_rule: string; credits_per_image: number }
export interface CatalogItem { id: string; name: string; category?: string }
export interface CatalogCategory { id: string; name: string; type: string; attires: CatalogItem[]; backgrounds: CatalogItem[] }
export interface Profile { user_id: string; email: string; full_name: string; credits_remaining: number; plan_name: string; plan: Plan; lora_status: 'none'|'training'|'ready'|'failed'; retrain: { count: number; free_left: number; cost: number } }
export interface UserJob { job_id: string; status: 'waiting_lora'|'queued'|'dispatching'|'processing'|'completed'|'failed'; job_type: string; category: string; output_blob_path: string[]|null; created_at: string }
export interface SubscriptionStatus { subscription_plan: string; subscription_type: string; credits_remaining: number; credits_monthly_limit: number; next_renewal: string|null; payment_failed: boolean; cancel_pending: boolean; cancel_at: string|null; queued_purchase: {type:string;plan:string}|null }
