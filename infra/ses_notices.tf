# Customer product notices (route retirements) send from their own subdomain,
# notices.trustedrouter.com, so their reputation and DMARC policy are isolated
# from sign-in mail (trustedrouter.com) and operator alerts
# (alerts.trustedrouter.com). This mirrors the alerts identity, which predates
# Terraform: Easy DKIM, a custom MAIL FROM, strict DMARC, and a configuration
# set that requires TLS, suppresses bounces and complaints, sends feedback to
# the shared ses-feedback topic, and reports reputation by mail class.

locals {
  notices_domain         = "notices.trustedrouter.com"
  notices_mail_from      = "mail.notices.trustedrouter.com"
  notices_dns_zone       = "trustedrouter-com"
  ses_feedback_topic_arn = "arn:aws:sns:us-east-1:330422590279:ses-feedback"
}

resource "aws_sesv2_configuration_set" "notices" {
  provider               = aws.us_east_1
  configuration_set_name = "trustedrouter-notices"

  delivery_options {
    tls_policy = "REQUIRE"
  }

  reputation_options {
    reputation_metrics_enabled = true
  }

  sending_options {
    sending_enabled = true
  }

  suppression_options {
    suppressed_reasons = ["BOUNCE", "COMPLAINT"]
  }
}

resource "aws_sesv2_configuration_set_event_destination" "notices_feedback_sns" {
  provider               = aws.us_east_1
  configuration_set_name = aws_sesv2_configuration_set.notices.configuration_set_name
  event_destination_name = "ses-feedback-sns"

  event_destination {
    enabled              = true
    matching_event_types = ["BOUNCE", "COMPLAINT"]

    sns_destination {
      topic_arn = local.ses_feedback_topic_arn
    }
  }
}

resource "aws_sesv2_configuration_set_event_destination" "notices_reputation" {
  provider               = aws.us_east_1
  configuration_set_name = aws_sesv2_configuration_set.notices.configuration_set_name
  event_destination_name = "reputation-by-mail-class"

  event_destination {
    enabled              = true
    matching_event_types = ["BOUNCE", "COMPLAINT", "DELIVERY", "REJECT", "SEND"]

    cloud_watch_destination {
      dimension_configuration {
        dimension_name          = "mail_class"
        dimension_value_source  = "MESSAGE_TAG"
        default_dimension_value = "unknown"
      }

      dimension_configuration {
        dimension_name          = "sender_profile"
        dimension_value_source  = "MESSAGE_TAG"
        default_dimension_value = "notices"
      }
    }
  }
}

resource "aws_sesv2_email_identity" "notices" {
  provider               = aws.us_east_1
  email_identity         = local.notices_domain
  configuration_set_name = aws_sesv2_configuration_set.notices.configuration_set_name
}

resource "aws_sesv2_email_identity_mail_from_attributes" "notices" {
  provider               = aws.us_east_1
  email_identity         = aws_sesv2_email_identity.notices.email_identity
  mail_from_domain       = local.notices_mail_from
  behavior_on_mx_failure = "REJECT_MESSAGE"
}

resource "google_dns_record_set" "notices_dkim" {
  count        = 3
  managed_zone = local.notices_dns_zone
  name         = "${aws_sesv2_email_identity.notices.dkim_signing_attributes[0].tokens[count.index]}._domainkey.${local.notices_domain}."
  type         = "CNAME"
  ttl          = 300
  rrdatas      = ["${aws_sesv2_email_identity.notices.dkim_signing_attributes[0].tokens[count.index]}.dkim.amazonses.com."]
}

resource "google_dns_record_set" "notices_mail_from_mx" {
  managed_zone = local.notices_dns_zone
  name         = "${local.notices_mail_from}."
  type         = "MX"
  ttl          = 300
  rrdatas      = ["10 feedback-smtp.us-east-1.amazonses.com."]
}

resource "google_dns_record_set" "notices_mail_from_spf" {
  managed_zone = local.notices_dns_zone
  name         = "${local.notices_mail_from}."
  type         = "TXT"
  ttl          = 300
  rrdatas      = ["\"v=spf1 include:amazonses.com ~all\""]
}

resource "google_dns_record_set" "notices_dmarc" {
  managed_zone = local.notices_dns_zone
  name         = "_dmarc.${local.notices_domain}."
  type         = "TXT"
  ttl          = 300
  rrdatas      = ["\"v=DMARC1; p=quarantine; adkim=s; aspf=r; pct=100\""]
}
