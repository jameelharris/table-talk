output "bucket_name" {
  value       = module.videos_bucket.name
  description = "Name of the videos GCS bucket."
}

output "bucket_url" {
  value       = module.videos_bucket.url
  description = "GCS URL of the videos bucket."
}

output "bucket_self_link" {
  value       = module.videos_bucket.self_link
  description = "Self-link of the GCS bucket."
}
