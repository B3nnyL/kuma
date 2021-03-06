import time
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core import mail
from django.test import Client
from django.urls import reverse
from stripe.error import APIError
from waffle.testutils import override_flag

from kuma.core.ga_tracking import (
    ACTION_SUBSCRIPTION_CANCELED,
    ACTION_SUBSCRIPTION_CREATED,
    CATEGORY_MONTHLY_PAYMENTS,
)
from kuma.core.utils import safer_pyquery as pq
from kuma.users.models import User, UserSubscription

from . import user


@dataclass
class StripeCustomerSource:
    object: str
    brand: str
    exp_month: int
    exp_year: int
    last4: int

    def get(self, key, default=None):
        return getattr(self, key, default)


@dataclass
class StripeCustomer:
    email: str
    default_source: StripeCustomerSource


@dataclass
class StripeSubscription:
    id: str
    current_period_end: int


def mock_get_stripe_customer(user):
    return StripeCustomer(
        email=user.email,
        default_source=StripeCustomerSource(
            object="card", brand="MagicCard", exp_month=12, exp_year=2020, last4=4242,
        ),
    )


def mock_get_stripe_subscription_info(customer, id="sub_123456789"):
    return StripeSubscription(id=id, current_period_end=time.time() + 10_000)


@pytest.fixture
def test_user(db, django_user_model):
    return User.objects.create(
        username="test_user",
        email="staff@example.com",
        date_joined=datetime(2019, 1, 17, 15, 42),
    )


@patch("kuma.users.views.create_stripe_customer_and_subscription_for_user")
@patch("kuma.users.views.get_stripe_customer")
@override_flag("subscription", True)
def test_create_stripe_subscription(mock1, mock2, test_user):
    customer = mock_get_stripe_customer(test_user)
    mock1.return_value = customer
    mock2.return_value = mock_get_stripe_subscription_info(customer)
    client = Client()
    client.force_login(test_user)

    response = client.post(
        reverse("users.create_stripe_subscription"),
        data={"stripe_token": "tok_visa", "stripe_email": "payer@example.com"},
        HTTP_HOST=settings.WIKI_HOST,
    )
    assert response.status_code == 302
    edit_profile_url = reverse("users.user_edit", args=[test_user.username])
    assert edit_profile_url in response["location"]
    assert response["location"].endswith("#subscription")


@patch("kuma.users.views.get_stripe_subscription_info")
@patch("kuma.users.views.get_stripe_customer")
@override_flag("subscription", True)
@pytest.mark.django_db
def test_user_edit_with_subscription_info(mock1, mock2, test_user):
    """The user has already signed up for a subscription and now the user edit
    page contains information about that from Stripe."""
    mock1.side_effect = mock_get_stripe_customer
    mock2.side_effect = mock_get_stripe_subscription_info
    client = Client()
    client.force_login(test_user)
    response = client.post(
        reverse("users.user_edit", args=[test_user.username]),
        HTTP_HOST=settings.WIKI_HOST,
    )
    assert response.status_code == 200
    page = pq(response.content)
    assert not page(".stripe-error").size()
    assert "MagicCard ending in 4242" in page(".card-info p").text()


@patch("kuma.users.stripe_utils.create_stripe_customer_and_subscription_for_user")
@patch(
    "kuma.users.stripe_utils.get_stripe_subscription_info",
    side_effect=mock_get_stripe_subscription_info,
)
@override_flag("subscription", False)
def test_create_stripe_subscription_fail(mock1, mock2, test_user):
    client = Client()
    client.force_login(test_user)
    response = client.post(
        reverse("users.create_stripe_subscription"),
        data={"stripe_token": "tok_visa", "stripe_email": "payer@example.com"},
        follow=True,
        HTTP_HOST=settings.WIKI_HOST,
    )
    assert response.status_code == 403


@patch("stripe.Event.construct_from")
@pytest.mark.django_db
def test_stripe_payment_succeeded_sends_invoice_mail(mock1, client):
    mock1.return_value = SimpleNamespace(
        type="invoice.payment_succeeded",
        data=SimpleNamespace(
            object=SimpleNamespace(
                customer="cus_mock_testuser",
                created=1583842724,
                invoice_pdf="https://developer.mozilla.org/mock-invoice-pdf-url",
            )
        ),
    )

    testuser = user(
        save=True,
        username="testuser",
        email="testuser@example.com",
        stripe_customer_id="cus_mock_testuser",
    )
    response = client.post(
        reverse("users.stripe_hooks"), content_type="application/json", data={},
    )
    assert response.status_code == 200
    assert len(mail.outbox) == 1
    payment_email = mail.outbox[0]
    assert payment_email.to == [testuser.email]
    assert "manage monthly subscriptions" in payment_email.body
    assert "invoice" in payment_email.subject


@patch("stripe.Event.construct_from")
@pytest.mark.django_db
def test_stripe_subscription_canceled(mock1, client):
    mock1.return_value = SimpleNamespace(
        type="customer.subscription.deleted",
        data=SimpleNamespace(
            object=SimpleNamespace(customer="cus_mock_testuser", id="sub_123456789")
        ),
    )

    testuser = user(
        save=True,
        username="testuser",
        email="testuser@example.com",
        stripe_customer_id="cus_mock_testuser",
    )
    UserSubscription.set_active(testuser, "sub_123456789")
    response = client.post(
        reverse("users.stripe_hooks"), content_type="application/json", data={},
    )
    assert response.status_code == 200
    (user_subscription,) = UserSubscription.objects.filter(user=testuser)
    assert user_subscription.canceled


@pytest.mark.django_db
def test_stripe_hook_invalid_json(client):
    response = client.post(
        reverse("users.stripe_hooks"),
        content_type="application/json",
        data="{not valid!",
    )
    assert response.status_code == 400


@patch("stripe.Event.construct_from")
@pytest.mark.django_db
def test_stripe_hook_unexpected_type(mock1, client):
    mock1.return_value = SimpleNamespace(
        type="not.expected", data=SimpleNamespace(foo="bar"),
    )
    response = client.post(
        reverse("users.stripe_hooks"), content_type="application/json", data={},
    )
    assert response.status_code == 400


@patch("stripe.Event.construct_from")
@pytest.mark.django_db
def test_stripe_hook_stripe_api_error(mock1, client):
    mock1.side_effect = APIError("badness")
    response = client.post(
        reverse("users.stripe_hooks"), content_type="application/json", data={},
    )
    assert response.status_code == 400


@patch("kuma.users.views.track_event")
@patch("stripe.Event.construct_from")
@pytest.mark.django_db
def test_stripe_payment_succeeded_sends_ga_tracking(
    mock1, track_event_mock_signals, client, settings
):
    settings.GOOGLE_ANALYTICS_ACCOUNT = "UA-XXXX-1"
    settings.GOOGLE_ANALYTICS_TRACKING_RAISE_ERRORS = True

    mock1.return_value = SimpleNamespace(
        type="invoice.payment_succeeded",
        data=SimpleNamespace(
            object=SimpleNamespace(
                customer="cus_mock_testuser",
                created=1583842724,
                invoice_pdf="https://developer.mozilla.org/mock-invoice-pdf-url",
            )
        ),
    )
    user(
        save=True,
        username="testuser",
        email="testuser@example.com",
        stripe_customer_id="cus_mock_testuser",
    )
    response = client.post(
        reverse("users.stripe_hooks"), content_type="application/json", data={},
    )
    assert response.status_code == 200

    track_event_mock_signals.assert_called_with(
        CATEGORY_MONTHLY_PAYMENTS,
        ACTION_SUBSCRIPTION_CREATED,
        f"{settings.CONTRIBUTION_AMOUNT_USD:.2f}",
    )


@patch("kuma.users.views.track_event")
@patch("stripe.Event.construct_from")
@pytest.mark.django_db
def test_stripe_subscription_canceled_sends_ga_tracking(
    mock1, track_event_mock_signals, client
):
    mock1.return_value = SimpleNamespace(
        type="customer.subscription.deleted",
        data=SimpleNamespace(
            object=SimpleNamespace(customer="cus_mock_testuser", id="sub_123456789")
        ),
    )

    user(
        save=True,
        username="testuser",
        email="testuser@example.com",
        stripe_customer_id="cus_mock_testuser",
    )
    response = client.post(
        reverse("users.stripe_hooks"), content_type="application/json", data={},
    )
    assert response.status_code == 200

    track_event_mock_signals.assert_called_with(
        CATEGORY_MONTHLY_PAYMENTS, ACTION_SUBSCRIPTION_CANCELED, "webhook"
    )
