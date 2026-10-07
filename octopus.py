import io
from pypdf import PdfReader
import re
import mariadb
import requests
import os
import cred_octopus
import cred_mariadb

SAVE_LOCATION = "C:\OctopusBills\\"


months = {"January": 1,
          "February": 2,
          "March": 3,
          "April": 4,
          "May": 5,
          "June": 6,
          "July": 7,
          "August": 8,
          "September": 9,
          "October": 10,
          "November": 11,
          "December": 12}

short_months = {"Jan.": 1,
                "Feb.": 2,
                "March": 3,
                "April": 4,
                "May": 5,
                "June": 6,
                "July": 7,
                "Aug.": 8,
                "Sept.": 9,
                "Oct.": 10,
                "Nov.": 11,
                "Dec.": 12}

endpoint = "https://api.octopus.energy/v1/graphql/"


# TODO - proper classes and methods!
# TODO - Add standing charge (per day - probably need a day table)
# TODO - Automatically calculate costs for other tariffs
# TODO - handle export

# Note that the following methods to retrieve bills from the Octopus API were stolen from:
# https://github.com/OllieJC/oebd
# You can find the Octopus GraphQL API documentation here: https://docs.octopus.energy/graphql/


def run_query(query, variables: dict = {}, with_auth: bool = True, force_auth: bool = False):
    headers = (
        {"Authorization": f"JWT {get_jwt(force=force_auth)}"}
        if with_auth or force_auth
        else {}
    )
    r = requests.post(
        endpoint, json={"query": query, "variables": variables}, headers=headers
    )
    if r.status_code == 200:
        return r.json() if r.text and r.text.startswith("{") else {}
    else:
        raise Exception(f"ERROR: non-200 response: {r.status_code}")


_jwt = None


def get_jwt(force: bool = False):
    global _jwt
    if not _jwt or force:
        _jwt = (
            run_query(
                """
                mutation krakenTokenAuthentication($APIKey: String!) {
                    obtainKrakenToken(input: {APIKey: $APIKey}) {
                        token
                    }
                }
                """,
                {"APIKey": cred_octopus.APIKey},
                with_auth=False,
            )
                .get("data", {})
                .get("obtainKrakenToken", {})
                .get("token", None)
        )
        if _jwt:
            print("INFO: Successfully signed in and acquired authentication token")
        else:
            raise Exception(f"ERROR: sign in or token acquisition failed")
    return _jwt


def get_accounts():
    return (
        run_query(
            """
            query get_accounts {
                viewer {
                    accounts {
                        brand
                        number
                        status
                        billingAddress
                        billingAddressLine1
                        billingAddressLine2
                        billingAddressLine3
                        billingAddressLine4
                        billingAddressLine5
                        billingAddressPostcode
                        billingCountryCode
                        accountType
                    }
                }
            }
            """,
            force_auth=True,
        )
            .get("data", {})
            .get("viewer", {})
            .get("accounts", [])
    )


def get_bills(account_number: str, count: int = 12):
    if not account_number:
        return {}

    return (
        run_query(
            """
            query get_bills($account_number: String!, $count: Int!) {
                account(accountNumber: $account_number) {
                    bills(first: $count, includeBillsWithoutPDF: false) {
                        edges {
                            node {
                                id
                                billType
                                temporaryUrl
                                issuedDate
                            }
                        }
                    }
                }
            }
            """,
            {"account_number": account_number, "count": count},
        )
            .get("data", {})
            .get("account", {})
            .get("bills", {})
            .get("edges", [])
    )


def process_bill(in_reader, in_bill_reference, in_bill_date):

    print(f"INFO: Processing bill {in_bill_reference} dated {in_bill_date}")

    first_day_page = 3
    meter_number = "<unknown>"

    while meter_number == "<unknown>" and first_day_page < len(in_reader.pages):

        pdf_page = in_reader.pages[first_day_page]
        text_content = pdf_page.extract_text().split()
        if text_content[38] == "meter":
            meter_number = text_content[39]

        first_day_page = first_day_page + 1

    my_cursor = mydb.cursor()

    sql = f"delete from octopus.bill where bill_reference = {in_bill_reference}"
    my_cursor.execute(sql)

    sql = "insert into octopus.bill (bill_reference, account_num, bill_date, meter_number) values (%s, %s, %s, %s)"
    values = (in_bill_reference, account_number, in_bill_date, meter_number)
    my_cursor.execute(sql, values)

    sql = f"delete from octopus.slot where bill_reference = {in_bill_reference}"
    my_cursor.execute(sql)

    sql = \
        "insert into octopus.slot (account_num, bill_reference, slot_date, slot_start, " + \
        "slot_price, slot_consumption, slot_total_cost)" + \
        " values (%s, %s, %s, %s, %s, %s, %s)"

    if meter_number == "<unknown>":
        print(f"WARNING: Bill {in_bill_reference} no detail")
        mydb.commit()
        return 1

    for page_num in range(first_day_page - 1, len(in_reader.pages)):
        pdf_page = in_reader.pages[page_num]

        text_content = pdf_page.extract_text().split()

        month_number = months[text_content[34]]
        day_of_month = re.split("[a-z]", text_content[33])[0]
        slot_date = text_content[35] + "-" + f"{month_number}" + "-" + day_of_month
        number_slots = 48

        # Check if clocks go back
        clocks_going_back = False
        if text_content[105] == "01:00":
            clocks_going_back = True
            number_slots = 50

        if text_content[93] == "02:00":
            number_slots = 46
            this_values = (account_number, in_bill_reference, slot_date,
                           "01:00", text_content[84], "0.00", "0.000")
            my_cursor.execute(sql, this_values)
            this_values = (account_number, in_bill_reference, slot_date,
                           "01:30", text_content[84], "0.00", "0.000")
            my_cursor.execute(sql, this_values)

        for half_hour in range(number_slots):
            this_values = (
                account_number, bill_reference, slot_date,
                text_content[81 + half_hour * 6],
                text_content[84 + half_hour * 6],
                text_content[85 + half_hour * 6],
                text_content[86 + half_hour * 6])

            if clocks_going_back:
                if half_hour in (2, 3):
                    continue
                elif half_hour in (4, 5):
                    this_values = (
                        account_number, in_bill_reference, slot_date,
                        text_content[81 + half_hour * 6],
                        text_content[84 + half_hour * 6],
                        float(text_content[85 + half_hour * 6]) + float(
                            text_content[85 + (half_hour - 2) * 6]),
                        float(text_content[86 + half_hour * 6]) + float(
                            text_content[86 + (half_hour - 2) * 6]))

            my_cursor.execute(sql, this_values)

    mydb.commit()

    return 0


mydb = mariadb.connect(host=cred_mariadb.maria_host, user=cred_mariadb.maria_user, password=cred_mariadb.maria_password)

for account in get_accounts():
    account_number = account.get("number", None)
    if account_number:
        print("INFO: Account:", account_number)
        for bill_node in get_bills(account_number=account_number, count=99):
            bill = bill_node.get("node", {})

            response = requests.get(bill.get("temporaryUrl"), stream=True)
            reader = PdfReader(io.BytesIO(response.content))

            bill_reference = bill["id"]
            bill_date = bill["issuedDate"]

            my_cursor = mydb.cursor()

            sql = f"select 1 from octopus.bill where bill_reference = {bill_reference}"
            my_cursor.execute(sql)
            my_result = my_cursor.fetchall()

            if len(my_result) > 0:
                print(f"INFO: Duplicate bill {bill_reference} ignored")

            else:
                bill_id = process_bill(reader, bill_reference, bill_date)

                full_path = os.path.join(SAVE_LOCATION, f"{account_number}-{bill_date}.pdf")
                with open(full_path, mode="wb") as file:
                    file.write(response.content)

