/* WdnddsString — minimal string TopicDataType for wdndds (FastDDS 2.6.x).

The FastDDS Python bindings don't ship a built-in string-typed
TopicDataType (the upstream ``eprosima::fastdds::dds::StringTopicDataType``
is C++-only, never SWIG-wrapped). This header declares a ~30-line
subclass that does CDR-style string encoding (u32 LE length + UTF-8
payload) so wdndds can publish JSON over DDS without going through
fastddsgen + IDL.

It also exposes a single C++ helper ``register_string_type(participant,
alias)`` that wraps the topic data type in a ``TypeSupport`` and
registers it — letting the Python side skip the broken
``fastdds.TypeSupport(TopicDataType *)`` SWIG overload entirely.

This variant targets FastDDS 2.6.x specifically:
- SerializedPayload_t lives in eprosima::fastrtps::rtps (not fastdds)
- TopicDataType API uses getKey(void*, InstanceHandle_t*, bool) not compute_key
- All payloads/handles are passed as pointers, not references
- DataRepresentationId_t does not exist on this API
- TopicDataType::setName(const char*) rather than set_name
*/

#pragma once

#include <fastdds/dds/domain/DomainParticipant.hpp>
#include <fastdds/dds/topic/TopicDataType.hpp>
#include <fastdds/dds/topic/TypeSupport.hpp>

#include <fastrtps/rtps/common/SerializedPayload.h>


namespace wdndds
{

class WdnddsStringTopicDataType : public eprosima::fastdds::dds::TopicDataType
{
public:
    WdnddsStringTopicDataType();

    ~WdnddsStringTopicDataType() override;

    bool serialize(
            void* data,
            eprosima::fastrtps::rtps::SerializedPayload_t* payload) override;

    bool deserialize(
            eprosima::fastrtps::rtps::SerializedPayload_t* payload,
            void* data) override;

    std::function<uint32_t()> getSerializedSizeProvider(
            void* data) override;

    void* createData() override;

    void deleteData(
            void* data) override;

    bool getKey(
            void* data,
            eprosima::fastrtps::rtps::InstanceHandle_t* ihandle,
            bool force_md5 = false) override;

private:
    static constexpr unsigned int MAX_STRING_SIZE = 4u * 1024u * 1024u;
};


/** Register the string type under a unique alias; return the alias as
    a heap-allocated ``const char*`` (callers must NOT free it).

    Returning ``const char*`` instead of ``std::string`` lets SWIG
    convert to a Python ``str`` cleanly — the latter needs an extra
    ownership tag and would otherwise leak the proxy.
*/
inline const char* register_string_type(
        eprosima::fastdds::dds::DomainParticipant& participant,
        const std::string& alias)
{
    auto* tdt = new WdnddsStringTopicDataType();
    static thread_local std::string type_name;
    type_name = "wdndds::string::" + alias;
    tdt->setName(type_name.c_str());
    auto support = std::make_shared<eprosima::fastdds::dds::TypeSupport>(tdt);
    participant.register_type(*support);
    // Thread-local so the pointer stays valid for the SWIG auto-convert
    // (Python takes a copy as str). Survives until the next call on the
    // same thread, which is fine for our usage.
    return type_name.c_str();
}


/** Create a new empty string sample for publishing. */
inline void* create_string_data()
{
    return new std::string();
}

/** Free a string sample after FastDDS is done with it. */
inline void delete_string_data(void* data)
{
    delete static_cast<std::string*>(data);
}

}  // namespace wdndds
